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

from news_intel.models import EventInput, EvidenceFact
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
    if entity:
        facts.append(EvidenceFact(id=_next_id(), kind="actor", text=entity))

    # Action/status (event type)
    event_label = event_type_label_zh_public(text) if is_zh else event_type_label_en(lowered)
    if event_label:
        facts.append(EvidenceFact(id=_next_id(), kind="action", text=event_label))

    # Numeric facts, all of them (not just the "winning" one) -- with role
    # where detectable, so the model has the same role-awareness the
    # deterministic renderer does rather than picking blind.
    if is_zh:
        for value in zh_amount_facts(text):
            facts.append(EvidenceFact(id=_next_id(), kind="numeric", text=f"{value:,.0f}", value=value, unit="CNY"))
    else:
        for value, role, _pos in dollar_facts_with_roles(text):
            facts.append(
                EvidenceFact(id=_next_id(), kind="numeric", text=f"${value:,.0f}", value=value, unit="USD", role=role)
            )

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
