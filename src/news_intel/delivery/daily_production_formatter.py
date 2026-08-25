"""Production daily digest renderer (2026-08-11 content-quality repair).

Takes a pipeline.daily_selection.SelectionResult (already deduplicated,
coverage-checked, and capped) and produces the final Telegram HTML-mode
text: clickable titles and source labels, TOP STORIES / MORE TO KNOW /
TOMORROW TO WATCH sections, and summary-quality guards -- a fallback
summary is used only if it is a complete, source-supported sentence that
does not end in an unresolved ellipsis and is not essentially a
restatement of the title; otherwise the summary line is omitted and only
the (linked) title/category/source remain. Never fabricates or
extrapolates missing facts.

All dynamic text is HTML-escaped before insertion; every link points at
the specific mapped article/filing/release URL already stored in the
database (event_repo/event_sources), never a URL supplied by this module
or invented -- see pipeline.daily_selection for how those URLs got there.
"""

from __future__ import annotations

import html

from news_intel.pipeline.daily_selection import UNDERFILLED_POOL_THRESHOLD, SelectionResult, Story
from news_intel.pipeline.render_readiness import chinese_takeaway, why_it_matters
from news_intel.pipeline.summary_quality import best_available_summary, extract_summary

# Backward-compatible alias -- the extraction logic now lives in
# pipeline/summary_quality.py (shared with daily_selection.py's TOP-
# eligibility check), but this module's own public name is unchanged.
summarize = extract_summary


def _escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def _link(text: str, url: str | None) -> str:
    if not url:
        return _escape(text)
    return f'<a href="{_escape(url)}">{_escape(text)}</a>'


def _primary_source(item: dict) -> dict | None:
    sources = item.get("sources") or []
    return sources[0] if sources and sources[0].get("url") else None


def _display_url(source: dict) -> str:
    """2026-08-15 publisher-family repair: the verified original article
    when event_repo.event_sources resolved one (`display_url`), else the
    stored discovery URL -- never a URL this module invents. Falls back to
    `url` for callers/fixtures predating the display_url field."""
    return source.get("display_url") or source["url"]


def _source_labels(story: Story) -> list[tuple[str, str]]:
    """Up to two (name, url) pairs -- the primary story's source, plus its
    linked secondary source if the family had one (2026-08-11: the second
    Daybreak article is retained as an additional linked source rather
    than a separate digest slot).

    2026-08-23 repair: real production regression (Aug 22 zh_cn digest, not
    yet delivered -- caught during the required content-integrity review)
    -- "Source: IT之家, IT之家" for two genuinely different, legitimately
    corroborating articles (different URLs, same publisher covering an
    evolving story -- the exact Daybreak-style case this function is
    designed to retain). The two links ARE both real and worth keeping,
    but identical display text gives a reader no way to tell them apart.
    Disambiguates the second label rather than dropping the link."""
    labels: list[tuple[str, str]] = []
    primary_src = _primary_source(story.primary)
    if primary_src:
        labels.append((primary_src.get("publisher") or "Source", _display_url(primary_src)))
    if story.secondary:
        secondary_src = _primary_source(story.secondary)
        if secondary_src and secondary_src["url"] != (primary_src or {}).get("url"):
            name = secondary_src.get("publisher") or "Source"
            if name == (primary_src or {}).get("publisher"):
                name = f"{name} (related coverage)"
            labels.append((name, _display_url(secondary_src)))
    return labels[:2]


def _source_line(story: Story) -> str | None:
    labels = _source_labels(story)
    if not labels:
        return None
    links = [_link(name, url) for name, url in labels]
    return "Source: " + ", ".join(links)


def _best_available_summary(story: Story) -> str | None:
    return best_available_summary(story.primary, story.secondary)


def _render_top_story(index: int, story: Story) -> list[str]:
    """2026-08-15 render-readiness repair: pipeline/daily_selection.py's
    _apply_render_readiness_gate already guarantees every story reaching
    this function has a complete "what happened" summary from at least one
    of its sources -- a story that didn't was demoted to MORE TO KNOW and
    replaced before this renders, so the old internal-diagnostic
    placeholder ("No complete source-supported summary available") can
    never legitimately appear here anymore. "Why it matters" and the
    Chinese takeaway are deterministic, template-based syntheses of
    already-verified structured facts (see pipeline/render_readiness.py's
    docstring for exactly what that does and does not claim)."""
    primary_src = _primary_source(story.primary)
    title_url = _display_url(primary_src) if primary_src else None
    lines = [
        "",
        f"{index}. {_link(story.primary.get('title', '(untitled)'), title_url)}",
        f"Category: {_escape(story.category)}",
    ]
    entities = story.primary.get("entities")
    editorial = story.editorial
    summary = editorial.what_happened if editorial else None
    if summary is None:
        summary = _best_available_summary(story)
    if summary:
        lines.append(_escape(summary))
    why = editorial.why_it_matters if editorial else why_it_matters(story.primary, story.secondary, story.category,
                                                                      entities)
    if why:
        lines.append(_escape(why))
    takeaway = editorial.chinese_takeaway if editorial else chinese_takeaway(story.category, entities, story.primary,
                                                                              story.secondary)
    if takeaway:
        lines.append(_escape(takeaway))
    if story.update_note:
        lines.append(_escape(story.update_note))
    source_line = _source_line(story)
    if source_line:
        lines.append(source_line)
    return lines


def _render_more_to_know(story: Story) -> str:
    primary_src = _primary_source(story.primary)
    title_url = _display_url(primary_src) if primary_src else None
    title_link = _link(story.primary.get("title", "(untitled)"), title_url)
    parts = [f"• {title_link}"]
    if story.update_note:
        parts.append(_escape(story.update_note))
    source_line = _source_line(story)
    if source_line:
        parts.append(source_line)
    return " — ".join(parts)


def render_production_daily(
    result: SelectionResult, date_label: str, calendar_items: list[dict]
) -> str:
    lines = [f"Daily Intelligence Brief — {date_label}", "", "This brief uses rule-ranked source summaries."]

    if result.top_stories:
        lines.append("")
        lines.append("TOP STORIES")
        for i, story in enumerate(result.top_stories, start=1):
            lines.extend(_render_top_story(i, story))

    if result.more_to_know:
        lines.append("")
        lines.append("MORE TO KNOW")
        for story in result.more_to_know:
            lines.append(_render_more_to_know(story))

    if not result.top_stories and not result.more_to_know:
        lines.append("")
        lines.append("No qualifying developments were found in the last 24 hours.")
    elif len(result.all_stories) < UNDERFILLED_POOL_THRESHOLD:
        lines.append("")
        lines.append(
            f"Note: only {len(result.all_stories)} stories met today's quality and source-diversity bar "
            "-- no lower-quality or promotional items were added just to fill out the digest."
        )

    if calendar_items:
        lines.append("")
        lines.append("TOMORROW TO WATCH")
        for cal in calendar_items:
            title = cal.get("title", "")
            url = cal.get("url")
            entry = _link(title, url) if url else _escape(title)
            time_label = cal.get("pacific_time_label")
            if time_label:
                entry += f" — {_escape(time_label)}"
            lines.append(f"• {entry}")

    return "\n".join(lines)
