"""Renders the deterministic monthly digest -- synthesizes the complete
previous calendar month (material changes, category rollups, noise vs.
signal, unresolved risks, next-month catalysts) from stored trend clusters;
never concatenates four weekly briefs verbatim. See jobs/monthly.py's
module docstring for why this has no LLM-attempt path (unlike
delivery/weekly_formatter.py's render_weekly, meant for an LLM-composed
`structured` dict) -- this is the single composition path, mirroring
llm/fallback.py's fallback_weekly_digest shape directly."""

from __future__ import annotations

from news_intel.delivery.splitter import label_parts, split_at_sections

MONTHLY_FALLBACK_NOTICE = (
    "AI summarization is not configured for this brief; this monthly report uses rule-ranked "
    "trend clusters instead of narrative synthesis."
)


def _monthly_header(month_label: str) -> str:
    return f"Monthly Intelligence Brief — {month_label}"


def render_monthly(month_label: str, trends: list[dict], prior_summary: dict, calendar_candidates: list[dict]) -> str:
    lines = [_monthly_header(month_label), "", MONTHLY_FALLBACK_NOTICE, ""]

    new_count = sum(1 for t in trends if t.get("status") == "new")
    continuing_count = sum(1 for t in trends if t.get("status") == "continuing")
    resolved_count = len(prior_summary.get("resolved_titles", []))
    lines.append("THIS MONTH'S CONCLUSIONS")
    lines.append(f"• {len(trends)} trends identified this month ({new_count} new).")
    lines.append(f"• {continuing_count} continuing, {resolved_count} resolved/cooled since last month.")
    signal_n = len(prior_summary.get("signal_titles", []))
    noise_n = len(prior_summary.get("noise_titles", []))
    lines.append(f"• {signal_n} signal events, {noise_n} noise/unconfirmed.")
    lines.append("")

    lines.append("WHAT MATERIALLY CHANGED THIS MONTH")
    changed = [t for t in trends if t.get("status") in ("new", "continuing")]
    changed.sort(key=lambda t: t.get("strength", 0), reverse=True)
    if changed:
        for i, t in enumerate(changed[:8], start=1):
            titles = "; ".join(t.get("titles", [])[:2])
            lines.append(f"{i}. [{t.get('category', 'uncategorized')}] {titles} (strength {t.get('strength', 0):.0f})")
    else:
        lines.append("No qualified trend changes this month.")
    lines.append("")

    for section_title, category in (
        ("AI & SEMICONDUCTORS", "AI & Semiconductors"),
        ("MARKETS & ECONOMY", "Markets & Economy"),
        ("US / CHINA / WORLD", "US / China / World"),
    ):
        lines.append(section_title)
        section_trends = [t for t in trends if t.get("category") == category]
        if not section_trends:
            lines.append("No qualified events this month.")
        else:
            for t in section_trends:
                titles = "; ".join(t.get("titles", [])[:3])
                lines.append(f"• {titles} (event ids: {', '.join(t.get('event_ids', []))})")
        lines.append("")

    lines.append("NOISE VS. SIGNAL")
    if prior_summary.get("signal_titles"):
        lines.append("Signal: " + "; ".join(prior_summary["signal_titles"][:6]))
    if prior_summary.get("noise_titles"):
        lines.append("Noise/unconfirmed: " + "; ".join(prior_summary["noise_titles"][:6]))
    if not prior_summary.get("signal_titles") and not prior_summary.get("noise_titles"):
        lines.append("Nothing notable to separate this month.")
    lines.append("")

    if prior_summary.get("unresolved_titles"):
        lines.append("UNRESOLVED RISKS")
        for title in prior_summary["unresolved_titles"][:8]:
            lines.append(f"• {title}")
        lines.append("")

    lines.append("NEXT MONTH")
    if calendar_candidates:
        for cal in calendar_candidates:
            lines.append(f"• {cal.get('title', '')} ({cal.get('scheduled_at', '')})")
    else:
        lines.append("No official calendar items on record for next month.")

    return "\n".join(lines)


def split_monthly(rendered_text: str, month_label: str, max_chars: int) -> list[str]:
    """At most two Telegram messages, same "split at the template's own
    natural boundary" approach as delivery/weekly_formatter.py's
    split_weekly."""
    if len(rendered_text) <= max_chars:
        return [rendered_text]

    header_line = _monthly_header(month_label)
    marker = "\nMARKETS & ECONOMY"
    idx = rendered_text.find(marker)
    if idx == -1:
        return label_parts(split_at_sections(rendered_text, max_chars))

    part1_body = rendered_text[:idx].rstrip()
    if part1_body.startswith(header_line):
        part1_body = part1_body[len(header_line):].lstrip("\n")
    part2_body = rendered_text[idx:].lstrip("\n")

    return [f"{header_line} (1/2)\n\n{part1_body}", f"{header_line} (2/2)\n\n{part2_body}"]
