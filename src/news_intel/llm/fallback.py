"""Deterministic fallback used when MiMo/the configured LLM is unavailable or
fails validation. Collection, clustering, scoring, and alert queueing are
unaffected by an LLM outage — only composition/enrichment degrades, and it
degrades to rule-ranked source titles/summaries rather than blocking the job
(design doc section 8.4)."""

from __future__ import annotations

from news_intel.models import DailyDigestInput, DigestOutput, EventEnrichment, EventInput

FALLBACK_NOTICE = (
    "AI summarization is temporarily unavailable; this brief uses the rule-ranked "
    "source summaries."
)


def fallback_enrich(event: EventInput) -> EventEnrichment:
    primary = event.sources[0] if event.sources else {}
    return EventEnrichment(
        event_id=event.event_id,
        category="uncategorized",
        subcategories=[],
        entities=[],
        english_headline=event.candidate_title[:140],
        what_happened=primary.get("excerpt", event.candidate_title)[:280],
        why_it_matters="Rule-based fallback: significance not summarized by the LLM.",
        chinese_headline=event.candidate_title[:60],
        chinese_takeaway="AI 摘要暂不可用，以下为规则排序的原始信息。",
        significance_proposal=0,
        market_impact_proposal=0,
        material_facts=[],
        uncertainties=["LLM enrichment unavailable; content is unsummarized source text."],
        supporting_source_ids=[s.get("id", "") for s in event.sources],
    )


def fallback_daily_digest(inputs: DailyDigestInput, date_label: str) -> DigestOutput:
    lines = [f"Daily Intelligence Brief — {date_label}", "", FALLBACK_NOTICE]
    if inputs.top3_candidates:
        lines.append("")
        lines.append("TOP 3")
    for i, ev in enumerate(inputs.top3_candidates[:3], start=1):
        lines.append(f"\n{i}. {ev.get('title', '(untitled)')}")
        lines.append(f"Category: {ev.get('category', 'uncategorized')}")
        if ev.get("excerpt"):
            lines.append(ev["excerpt"][:280])
        sources = ", ".join(s.get("publisher", "") for s in ev.get("sources", []))
        if sources:
            lines.append(f"Sources: {sources}")

    if inputs.quick_brief_candidates:
        lines.append("\nQUICK BRIEFS")
        for ev in inputs.quick_brief_candidates:
            lines.append(f"• [{ev.get('category', 'uncategorized')}] {ev.get('title', '(untitled)')}")

    if inputs.calendar_candidates:
        lines.append("\nTOMORROW TO WATCH")
        for cal in inputs.calendar_candidates:
            lines.append(f"• {cal.get('title', '')}")

    return DigestOutput(
        rendered_text="\n".join(lines),
        model_used="none (deterministic fallback)",
        fallback_used=True,
    )
