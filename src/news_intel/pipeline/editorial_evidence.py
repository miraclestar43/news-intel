"""Builds the bounded evidence packet (models.EventInput) fed to the
optional per-story LLM editorial layer (2026-08-24).

This module does no extraction of its own -- it reuses the SAME
deterministic extractors that already produce the deterministic-floor
why_it_matters()/chinese_takeaway() output (materiality.py, render_
readiness.py, zh_render_readiness.py's public wrapper functions), just
packaged as a set of stable, id-tagged EvidenceFact rows instead of a
rendered sentence. The model is asked to compose FROM these facts and cite
which ones it used -- it is never asked to re-extract facts from raw
article text itself, which is what narrows an LLM failure mode down to
"fluent composition of a wrong sentence" rather than "invented a number."
"""

from __future__ import annotations

import re
from datetime import datetime

from news_intel.models import EventInput, EvidenceFact
from news_intel.pipeline.entities import extract_entities
from news_intel.pipeline.render_readiness import (
    dollar_facts_with_roles,
    event_type_label_en,
    principal_entity_en,
)
from news_intel.pipeline.zh_render_readiness import (
    _combined_text as _combined_text_zh,
)
from news_intel.pipeline.zh_render_readiness import (
    event_type_label_zh_public,
    principal_entity_zh,
    zh_amount_facts,
)
from news_intel.pipeline.zh_text import strip_zh_trailer

# 2026-11-01 (news-intel v2): bounds for the per-source evidence offered to the model. Every source of the event (and
# of any same-event cluster merged in from the other edition) is offered, distinct source families first, so a
# headline-only figure ("More than 5,000 die from Ebola") and a second outlet's account are both in front of the model
# and of the validator. The prompt stays inside llm/hermes_cli._MAX_PROMPT_CHARS.
MAX_SOURCES = 6
MAX_EXCERPT_CHARS = 900
_CJK_RE = re.compile(r"[一-鿿]")

_UNCERTAINTY_MARKERS_EN = (
    "could", "might", "may ", "discussed", "in talks", "considering", "exploring",
    "potential", "reportedly", "sources say", "sources said", "alleged", "unconfirmed",
)
_UNCERTAINTY_MARKERS_ZH = ("或将", "据报道", "消息称", "据悉", "传闻", "疑似", "尚未证实", "初步")

# 2026-08-25 evidence-packaging repair: a real live-model replay across
# real production stories found the evidence packet only ever offered ONE
# actor fact (the single "principal entity") -- named banks, investors,
# counterparties, and exchanges present in the source text had no
# corresponding fact at all, so the model's (accurate) mentions of them
# were rejected as untraceable. These extend actor packaging generally
# (not per-story), reusing the SAME deterministic extractors already
# relied on elsewhere in this pipeline.
_BARE_MAGNITUDE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(million|billion|trillion|mn|bn|tn|M|B|T)\b")
_BARE_MAGNITUDE_MULTIPLIERS = {
    "million": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "trillion": 1_000_000_000_000, "tn": 1_000_000_000_000, "t": 1_000_000_000_000,
}

# Chinese has no capitalization signal to lean on the way English does, so
# a general organization-name extractor instead anchors on common
# corporate/institutional suffixes and takes the immediately preceding
# 2-8 CJK characters as the candidate name (e.g. "高盛" before no suffix
# is itself the whole name already handled by known-entity matching
# elsewhere; "泰康人寿" ends in "人寿"). General, not tied to any one
# story's specific institutions.
_ZH_ORG_SUFFIXES = ("银行", "证券", "集团", "资本", "保险", "基金", "交易所", "控股", "传媒", "科技", "公司", "人寿")
_ZH_ORG_RE = re.compile(
    r"[一-鿿]{1,6}(?:" + "|".join(_ZH_ORG_SUFFIXES) + r")"
)


def _bare_magnitude_numeric_facts_en(text: str, already: set[float]) -> list[EvidenceFact]:
    """Numbers with an M/B/T-style magnitude suffix but NO leading $ (a
    share count, a user count, ...) -- render_readiness.dollar_facts_with_
    roles only ever matches $-anchored figures, so "710M shares" had no
    corresponding fact at all even though it's a real, traceable number in
    the source text. `already` is the set of dollar-fact values already
    packaged, so the same figure is never offered twice under two facts."""
    facts = []
    for match in _BARE_MAGNITUDE_RE.finditer(text):
        preceding = text[max(0, match.start() - 2) : match.start()]
        if "$" in preceding:
            continue  # already captured by dollar_facts_with_roles
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        value *= _BARE_MAGNITUDE_MULTIPLIERS[match.group(2).lower()]
        if value in already:
            continue
        already.add(value)
        facts.append(EvidenceFact(id="", kind="numeric", text=f"{value:,.0f}", value=value))
    return facts


def _combined_text_en(primary: dict, secondary: dict | None) -> str:
    parts = [f"{primary.get('title', '')} {primary.get('excerpt', '') or ''}"]
    if secondary:
        parts.append(f"{secondary.get('title', '')} {secondary.get('excerpt', '') or ''}")
    return " ".join(parts)


def _trim(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Cut at the last sentence end inside the limit (never mid-sentence when one exists)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(p) for p in ("。", "！", "？", ". ", "! ", "? "))
    return head[: cut + 1].strip() if cut > limit // 3 else head.rstrip() + "…"


def offered_sources(events: list[dict], max_sources: int = MAX_SOURCES) -> list[dict]:
    """Every source of the given same-event clusters (the first is the primary), one per article URL, distinct
    origin families first. Each keeps its own title -- the headline is evidence too (2026-11-01: the Ebola story's
    death toll was only in its BBC headline, which neither the model nor the validator ever saw)."""
    seen_urls: set[str] = set()
    pool: list[dict] = []
    for ev in events:
        for j, src in enumerate(ev.get("sources") or []):
            url = src.get("display_url") or src.get("url") or ""
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            if j == 0 and not (src.get("raw_title") or src.get("origin_title") or src.get("title")):
                src = {**src, "title": ev.get("title", "")}  # a source row without its own title: the event's
            pool.append(src)
    first_per_family: list[dict] = []
    rest: list[dict] = []
    families: set[str] = set()
    for src in pool:
        fam = src.get("origin_family") or src.get("publisher") or ""
        (rest if fam in families else first_per_family).append(src)
        families.add(fam)
    chosen = (first_per_family + rest)[:max_sources]
    out = []
    for i, src in enumerate(chosen):
        title = src.get("raw_title") or src.get("origin_title") or src.get("title") or ""
        excerpt = src.get("excerpt") or ""
        if excerpt.strip() == title.strip():
            excerpt = ""
        lang = "zh" if _CJK_RE.search(f"{title}{excerpt}") else "en"
        out.append({
            "id": f"s{i}",
            "publisher": src.get("publisher", ""),
            "title": title,
            "excerpt": _trim(strip_zh_trailer(excerpt) if lang == "zh" else excerpt),
            "published_at": str(src.get("published_at") or ""),
            "url": src.get("display_url") or src.get("url", ""),
            "language": lang,
        })
    return out


def build_event_input(
    primary: dict, secondary: dict | None, category: str, entities: list[str] | None,
    *, edition_id: str = "en_global", update_note: str | None = None, related: list[dict] | None = None,
    reference_time: datetime | None = None,
) -> EventInput:
    """`related`: other events of the same real-world story (the brief merges both editions' clusters); their sources
    are offered too. `reference_time`: the run's cutoff, so the model reads source dates against it."""
    is_zh = edition_id == "zh_cn"
    clusters = [primary, *([secondary] if secondary else []), *(related or [])]
    sources = offered_sources(clusters)
    text = " ".join(f"{s['title']} {s['excerpt']}" for s in sources) or (
        _combined_text_zh(primary, secondary) if is_zh else _combined_text_en(primary, secondary)
    )
    lowered = text.lower()

    facts: list[EvidenceFact] = []
    fact_idx = 0

    def _next_id() -> str:
        nonlocal fact_idx
        fact_idx += 1
        return f"f{fact_idx}"

    # Actor/target/subject
    entity = principal_entity_zh(entities, text) if is_zh else principal_entity_en(entities, text, primary.get("title"))
    actor_texts_seen: set[str] = set()
    if entity:
        facts.append(EvidenceFact(id=_next_id(), kind="actor", text=entity))
        actor_texts_seen.add(entity)

    # 2026-08-25 evidence-packaging repair: a real live-model replay found
    # only the SINGLE principal entity was ever offered as an actor fact --
    # named banks, investors, counterparties, and exchanges present in the
    # source text (e.g. underwriters in a share-placement story) had no
    # fact at all, so the model's accurate mentions of them were rejected
    # as untraceable. Package every already-extracted entity (entities.py's
    # extract_entities already ran upstream to produce this list -- this
    # module does no NEW extraction of its own here, just wider reuse of
    # it), plus (English) a fresh capitalized-proper-noun pass over the
    # combined text to catch names the upstream extractor's known-
    # vocabulary matching missed, plus (Chinese) a general organization-
    # suffix pass. Capped so the packet stays bounded.
    for name in (entities or [])[:12]:
        if name in actor_texts_seen:
            continue
        actor_texts_seen.add(name)
        facts.append(EvidenceFact(id=_next_id(), kind="actor", text=name))
    if is_zh:
        for m in _ZH_ORG_RE.finditer(text):
            name = m.group(0)
            if name in actor_texts_seen or len(actor_texts_seen) >= 15:
                continue
            actor_texts_seen.add(name)
            facts.append(EvidenceFact(id=_next_id(), kind="actor", text=name))
    else:
        for name in extract_entities(text, known=set()):
            if name in actor_texts_seen or len(actor_texts_seen) >= 15:
                continue
            actor_texts_seen.add(name)
            facts.append(EvidenceFact(id=_next_id(), kind="actor", text=name))

    # Action/status (event type)
    event_label = event_type_label_zh_public(text) if is_zh else event_type_label_en(lowered)
    if event_label:
        facts.append(EvidenceFact(id=_next_id(), kind="action", text=event_label))

    # Numeric facts, all of them (not just the "winning" one) -- with role
    # where detectable, so the model has the same role-awareness the
    # deterministic renderer does rather than picking blind.
    numeric_values_seen: set[float] = set()
    if is_zh:
        for value in zh_amount_facts(text):
            facts.append(EvidenceFact(id=_next_id(), kind="numeric", text=f"{value:,.0f}", value=value, unit="CNY"))
            numeric_values_seen.add(value)
    else:
        for value, role, _pos in dollar_facts_with_roles(text):
            facts.append(
                EvidenceFact(id=_next_id(), kind="numeric", text=f"${value:,.0f}", value=value, unit="USD", role=role)
            )
            numeric_values_seen.add(value)
        # Bare magnitude figures with no leading $ (a share count, a user
        # count, ...) -- dollar_facts_with_roles only matches $-anchored
        # figures.
        for fact in _bare_magnitude_numeric_facts_en(text, numeric_values_seen)[:6]:
            facts.append(fact.model_copy(update={"id": _next_id()}))

    # Uncertainty/attribution
    markers = _UNCERTAINTY_MARKERS_ZH if is_zh else _UNCERTAINTY_MARKERS_EN
    hedges = [m for m in markers if m in (text if is_zh else lowered)]
    if hedges:
        facts.append(
            EvidenceFact(id=_next_id(), kind="uncertainty", text="prospective/unconfirmed language present: "
                         + ", ".join(hedges[:3]))
        )

    # Prior-coverage delta, if daily_selection.py's cross-day novelty check
    # already found one (Story.update_note) -- never invented here.
    if update_note:
        facts.append(EvidenceFact(id=_next_id(), kind="prior_coverage", text=update_note))

    if not sources:  # an event dict with no source rows (tests, legacy callers): its own title/excerpt is the source
        sources = [{
            "id": "s0", "publisher": "", "title": primary.get("title", ""),
            "excerpt": _trim(primary.get("excerpt", "")), "published_at": "", "url": "",
            "language": "zh" if is_zh else "en",
        }]

    return EventInput(
        event_id=primary.get("event_id", ""),
        candidate_title=primary.get("title", ""),
        sources=sources,
        edition_id=edition_id,
        category_candidates=[category] if category else [],
        facts=facts,
        reference_time=reference_time.isoformat() if reference_time else None,
    )
