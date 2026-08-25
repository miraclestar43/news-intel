"""Builds the exact enrich_event prompt sent to any LLMProvider (2026-08-24)
-- shared by every provider implementation (OpenAICompatible, HermesCli,
...) so every provider sees the SAME evidence packet in the SAME format.

Fixes a gap where EventInput.facts/category_candidates were collected by
pipeline.editorial_evidence.build_event_input but never actually rendered
into the prompt text: a model that never sees the offered fact ids can
never legitimately produce a non-empty, non-fabricated cited_fact_ids,
which pipeline.editorial_validation.validate_enrichment's check #2
requires -- every real LLM call was therefore guaranteed to fail
validation regardless of provider or model quality until this was fixed.
"""

from __future__ import annotations

from news_intel.llm.prompts import load_template, render, render_sources_block
from news_intel.models import EventInput, EvidenceFact


def render_facts_block(facts: list[EvidenceFact]) -> str:
    if not facts:
        return "(none offered)"
    lines = []
    for f in facts:
        attrs = [f'id="{f.id}"', f'kind="{f.kind}"']
        if f.value is not None:
            attrs.append(f"value={f.value}")
        if f.unit:
            attrs.append(f'unit="{f.unit}"')
        if f.role:
            attrs.append(f'role="{f.role}"')
        if f.source_id:
            attrs.append(f'source_id="{f.source_id}"')
        lines.append(f"- {' '.join(attrs)}: {f.text}")
    return "\n".join(lines)


def build_enrich_event_prompt(event: EventInput) -> str:
    template = load_template("enrich_event.md")
    return render(
        template,
        event_id=event.event_id,
        sources_block=render_sources_block(event.sources),
        facts_block=render_facts_block(event.facts),
        category_candidates_block=", ".join(event.category_candidates) or "(none provided)",
    )
