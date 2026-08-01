"""Renders the LLM-composed weekly digest (design doc section 4.3) from the
structured JSON shape produced by compose_weekly.md."""

from __future__ import annotations

from news_intel.delivery.splitter import label_parts, split_at_sections


def render_weekly(week_label: str, structured: dict) -> str:
    lines = [f"Weekly Intelligence Brief — {week_label}", ""]

    synthesis = structured.get("chinese_synthesis", [])
    if synthesis:
        lines.append("本周结论")
        for item in synthesis:
            lines.append(f"• {item}")
        lines.append("")

    lines.append("WHAT MATERIALLY CHANGED")
    for i, item in enumerate(structured.get("materially_changed", []), start=1):
        lines.append(f"{i}. {item}")
    lines.append("")

    lines.append("AI & SEMICONDUCTORS")
    lines.append(structured.get("ai_semiconductors", "No qualified events this week."))
    lines.append("")

    lines.append("MARKETS & ECONOMY")
    lines.append(structured.get("markets_economy", "No qualified events this week."))
    lines.append("")

    lines.append("US / CHINA / WORLD")
    lines.append(structured.get("us_china_world", "No qualified events this week."))
    lines.append("")

    lines.append("NOISE VS. SIGNAL")
    lines.append(structured.get("noise_vs_signal", "Nothing notable to separate this week."))
    lines.append("")

    unresolved = structured.get("unresolved", [])
    if unresolved:
        lines.append("UNRESOLVED / DEVELOPING STORIES")
        for item in unresolved:
            lines.append(f"• {item}")
        lines.append("")

    lines.append("NEXT WEEK")
    next_week = structured.get("next_week", [])
    if next_week:
        for item in next_week:
            lines.append(f"• {item}")
    else:
        lines.append("No official calendar items on record for next week.")

    return "\n".join(lines)


def split_weekly(rendered_text: str, week_label: str, max_chars: int) -> list[str]:
    """At most two Telegram messages, labeled (1/2) and (2/2), splitting at
    the template's own natural boundary (design doc section 4.3) rather than
    a generic size-based cut. Falls back to the generic section splitter if
    the expected boundary marker isn't present (e.g. an unusually shaped
    fallback render)."""
    if len(rendered_text) <= max_chars:
        return [rendered_text]

    header_line = f"Weekly Intelligence Brief — {week_label}"
    marker = "\nMARKETS & ECONOMY"
    idx = rendered_text.find(marker)
    if idx == -1:
        return label_parts(split_at_sections(rendered_text, max_chars))

    part1_body = rendered_text[:idx].rstrip()
    if part1_body.startswith(header_line):
        part1_body = part1_body[len(header_line):].lstrip("\n")
    part2_body = rendered_text[idx:].lstrip("\n")

    return [f"{header_line} (1/2)\n\n{part1_body}", f"{header_line} (2/2)\n\n{part2_body}"]
