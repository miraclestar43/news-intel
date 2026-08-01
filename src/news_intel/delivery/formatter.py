"""Renders the structured daily digest (LLM output or deterministic fallback)
into the Telegram daily-brief template (design doc section 4.2)."""

from __future__ import annotations


def render_daily(date_label: str, structured: dict) -> str:
    lines = [f"Daily Intelligence Brief — {date_label}"]

    top3 = structured.get("top3", [])[:3]
    if top3:
        lines.append("")
        lines.append("TOP 3")
    for i, ev in enumerate(top3, start=1):
        lines.append("")
        lines.append(f"{i}. {ev.get('english_headline', '')} / {ev.get('chinese_headline', '')}")
        lines.append(f"What happened: {ev.get('what_happened', '')}")
        lines.append(f"Why it matters: {ev.get('why_it_matters', '')}")
        lines.append(f"中文要点：{ev.get('chinese_takeaway', '')}")
        sources = " · ".join(ev.get("sources", []))
        if sources:
            lines.append(f"Sources: {sources}")

    quick = structured.get("quick_briefs", [])
    if quick:
        lines.append("")
        lines.append("QUICK BRIEFS")
        by_section: dict[str, list[dict]] = {}
        for item in quick:
            by_section.setdefault(item.get("section", "Other"), []).append(item)
        for section, items in by_section.items():
            for item in items:
                lines.append("")
                lines.append(f"• {section} — {item.get('english_summary', '')}")
                if item.get("chinese_takeaway"):
                    lines.append(f"  中文：{item['chinese_takeaway']}")

    watch = structured.get("tomorrow_to_watch", [])
    if watch:
        lines.append("")
        lines.append("TOMORROW TO WATCH")
        for w in watch:
            lines.append(f"• {w}")

    return "\n".join(lines)
