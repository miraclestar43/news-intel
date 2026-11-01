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


_LANGUAGE_ZH = """Language: this event is for the CHINESE brief. Write chinese_headline (at most 28 characters), \
chinese_what_happened (1-3 sentences, at most 170 characters: the development, who and when, the key figure, and \
what is new today; never repeat a fact or pad to reach a length), chinese_brief_line, and chinese_takeaway only if \
there is a supported why \
line. Leave english_headline, what_happened and why_it_matters as "". Write natural simplified Chinese for a general \
reader, even when the sources are in English or traditional Chinese. Use the Chinese name a source gives, or the \
standard Chinese name of a well-known organisation, person or place; keep a name in Latin letters only when it has no \
established Chinese name. Do NOT add English names, translations or abbreviations in parentheses after a Chinese name \
(write 国铁集团, not "国铁集团 (China State Railway Group)")."""

_LANGUAGE_EN = """Language: this event is for the ENGLISH brief. Write english_headline (at most 140 characters), \
what_happened and, only if there is a supported why line, why_it_matters. Also write chinese_headline, \
chinese_what_happened and (only with a supported why line) chinese_takeaway as natural simplified Chinese carrying \
the same facts, without English names in parentheses after Chinese names. chinese_brief_line may be ""."""


def render_retry_block(feedback: list[str]) -> str:
    """2026-11-01: the validator's reasons for rejecting the previous attempt, as an explicit instruction. Before, they
    were appended to candidate_title, which this prompt never rendered -- every retry repeated the same prompt."""
    if not feedback:
        return ""
    lines = "\n".join(f"- {r}" for r in feedback[:12])
    return (
        "\nRETRY: your previous answer for this event was rejected by the automatic checker for these reasons:\n"
        f"{lines}\n"
        "Write a corrected answer from the same sources. Remove or fix every rejected element; do not add anything "
        "the sources do not support in its place. If a claim cannot be supported by a verbatim quote, leave it out.\n"
    )


def build_enrich_event_prompt(event: EventInput) -> str:
    template = load_template("enrich_event.md")
    return render(
        template,
        event_id=event.event_id,
        reference_time=event.reference_time or "(not given: use the sources' own dates)",
        language_block=_LANGUAGE_ZH if event.edition_id == "zh_cn" else _LANGUAGE_EN,
        sources_block=render_sources_block(event.sources),
        facts_block=render_facts_block(event.facts),
        category_candidates_block=", ".join(event.category_candidates) or "(none provided)",
        retry_block=render_retry_block(event.retry_feedback),
    )
