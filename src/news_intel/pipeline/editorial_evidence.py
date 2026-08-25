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


def build_event_input(
    primary: dict, secondary: dict | None, category: str, entities: list[str] | None,
    *, edition_id: str = "en_global", update_note: str | None = None,
) -> EventInput:
    is_zh = edition_id == "zh_cn"
    text = _combined_text_zh(primary, secondary) if is_zh else _combined_text_en(primary, secondary)
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

    sources = []
    for i, src in enumerate((primary, *([secondary] if secondary else []))):
        source_list = src.get("sources") or []
        source = source_list[0] if source_list else {}
        sources.append(
            {
                "id": f"s{i}",
                "publisher": source.get("publisher", ""),
                "excerpt": src.get("excerpt") or source.get("excerpt", ""),
                "published_at": str(source.get("published_at", "")),
                "url": source.get("display_url") or source.get("url", ""),
            }
        )

    return EventInput(
        event_id=primary.get("event_id", ""),
        candidate_title=primary.get("title", ""),
        sources=sources,
        edition_id=edition_id,
        category_candidates=[category] if category else [],
        facts=facts,
    )
