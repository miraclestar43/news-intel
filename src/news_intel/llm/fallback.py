"""Deterministic fallback used when MiMo/the configured LLM is unavailable or
fails validation. Collection, clustering, scoring, and alert queueing are
unaffected by an LLM outage — only composition/enrichment degrades, and it
degrades to rule-ranked source titles/summaries rather than blocking the job
(design doc section 8.4)."""

from __future__ import annotations

from news_intel.models import DailyDigestInput, DigestOutput, EventEnrichment, EventInput, WeeklyDigestInput

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


WEEKLY_FALLBACK_NOTICE = (
    "AI summarization is temporarily unavailable; this weekly brief uses rule-ranked "
    "trend clusters instead of narrative synthesis."
)


def fallback_weekly_digest(inputs: WeeklyDigestInput, week_label: str) -> DigestOutput:
    """Deterministic weekly brief: no narrative synthesis capability without
    an LLM, but every section is still populated from stored trend clusters,
    comparisons, and calendar rows -- never blocks the Sunday send."""
    trends = inputs.events
    summary = inputs.prior_week_summary

    lines = [f"Weekly Intelligence Brief — {week_label}", "", WEEKLY_FALLBACK_NOTICE, ""]

    lines.append("本周结论")
    new_count = sum(1 for t in trends if t.get("status") == "new")
    continuing_count = sum(1 for t in trends if t.get("status") == "continuing")
    resolved_count = len(summary.get("resolved_titles", []))
    lines.append(f"• 本周共识别 {len(trends)} 个趋势，其中 {new_count} 个为新趋势。")
    lines.append(f"• {continuing_count} 个趋势持续，{resolved_count} 个上周趋势已解决或降温。")
    signal_n = len(summary.get("signal_titles", []))
    noise_n = len(summary.get("noise_titles", []))
    lines.append(f"• 信号事件 {signal_n} 个，噪音/未证实 {noise_n} 个。")
    lines.append("")

    lines.append("WHAT MATERIALLY CHANGED")
    changed = [t for t in trends if t.get("status") in ("new", "continuing")]
    changed.sort(key=lambda t: t.get("strength", 0), reverse=True)
    if changed:
        for i, t in enumerate(changed[:6], start=1):
            titles = "; ".join(t.get("titles", [])[:2])
            lines.append(f"{i}. [{t.get('category', 'uncategorized')}] {titles} (strength {t.get('strength', 0):.0f})")
    else:
        lines.append("No qualified trend changes this week.")
    lines.append("")

    for section_title, category in (
        ("AI & SEMICONDUCTORS", "AI & Semiconductors"),
        ("MARKETS & ECONOMY", "Markets & Economy"),
        ("US / CHINA / WORLD", "US / China / World"),
    ):
        lines.append(section_title)
        section_trends = [t for t in trends if t.get("category") == category]
        if not section_trends:
            lines.append("No qualified events this week.")
        else:
            for t in section_trends:
                titles = "; ".join(t.get("titles", [])[:3])
                lines.append(f"• {titles} (event ids: {', '.join(t.get('event_ids', []))})")
        lines.append("")

    lines.append("NOISE VS. SIGNAL")
    if summary.get("signal_titles"):
        lines.append("Signal: " + "; ".join(summary["signal_titles"][:5]))
    if summary.get("noise_titles"):
        lines.append("Noise/unconfirmed: " + "; ".join(summary["noise_titles"][:5]))
    if not summary.get("signal_titles") and not summary.get("noise_titles"):
        lines.append("Nothing notable to separate this week.")
    if summary.get("became_policy_titles") or summary.get("remained_statement_titles"):
        lines.append(
            "Became formal policy: " + (", ".join(summary.get("became_policy_titles", [])) or "none")
        )
        lines.append(
            "Remained statement only: " + (", ".join(summary.get("remained_statement_titles", [])) or "none")
        )
    lines.append("")

    if summary.get("unresolved_titles"):
        lines.append("UNRESOLVED / DEVELOPING STORIES")
        for title in summary["unresolved_titles"][:6]:
            lines.append(f"• {title}")
        lines.append("")

    lines.append("NEXT WEEK")
    if inputs.calendar_candidates:
        for cal in inputs.calendar_candidates:
            lines.append(f"• {cal.get('title', '')} ({cal.get('scheduled_at', '')})")
    else:
        lines.append("No official calendar items on record for next week.")

    return DigestOutput(
        rendered_text="\n".join(lines),
        model_used="none (deterministic fallback)",
        fallback_used=True,
    )
