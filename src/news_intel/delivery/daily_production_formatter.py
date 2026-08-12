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
import re

from news_intel.pipeline.daily_selection import SelectionResult, Story
from news_intel.pipeline.normalize import normalize_title
from news_intel.pipeline.similarity import trigram_jaccard

SUMMARY_MAX_CHARS = 280
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
_ELLIPSIS_SUFFIXES = ("...", "…")
_REDUNDANCY_TRIGRAM_FLOOR = 0.75


def _strip_trailing_ellipsis(text: str) -> str:
    """A trailing '...' or '…' is the source's own truncation marker, not
    sentence-ending punctuation -- whatever precedes it is never treated as
    complete just because a period-like character happens to follow."""
    stripped = text
    changed = True
    while changed:
        changed = False
        for suffix in _ELLIPSIS_SUFFIXES:
            rs = stripped.rstrip()
            if rs.endswith(suffix):
                stripped = rs[: -len(suffix)]
                changed = True
    return stripped


def _complete_sentences(excerpt: str, max_chars: int) -> list[str]:
    window = _strip_trailing_ellipsis(excerpt[:max_chars])
    matches = list(_SENTENCE_END_RE.finditer(window))
    sentences = []
    start = 0
    for m in matches:
        s = window[start : m.end()].strip()
        if s:
            sentences.append(s)
        start = m.end()
    return sentences


def _is_redundant_with_title(candidate: str, title: str) -> bool:
    if not candidate or not title:
        return False
    return trigram_jaccard(normalize_title(candidate), normalize_title(title)) >= _REDUNDANCY_TRIGRAM_FLOOR


def summarize(excerpt: str | None, title: str, *, max_chars: int = SUMMARY_MAX_CHARS) -> str | None:
    """None means: omit the summary line entirely (title/category/source
    are still shown) -- never a truncated fragment, never a restatement of
    the title, never an invented fact."""
    if not excerpt:
        return None
    sentences = _complete_sentences(excerpt, max_chars)
    if not sentences:
        return None
    # Redundancy is judged on the leading sentence specifically -- a
    # multi-sentence summary that merely *starts* by restating the title
    # should drop that opening sentence, not be rejected wholesale.
    if not _is_redundant_with_title(sentences[0], title):
        return " ".join(sentences)
    if len(sentences) > 1:
        rest = " ".join(sentences[1:])
        if rest and not _is_redundant_with_title(sentences[1], title):
            return rest
    return None


def _escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def _link(text: str, url: str | None) -> str:
    if not url:
        return _escape(text)
    return f'<a href="{_escape(url)}">{_escape(text)}</a>'


def _primary_source(item: dict) -> dict | None:
    sources = item.get("sources") or []
    return sources[0] if sources and sources[0].get("url") else None


def _source_labels(story: Story) -> list[tuple[str, str]]:
    """Up to two (name, url) pairs -- the primary story's source, plus its
    linked secondary source if the family had one (2026-08-11: the second
    Daybreak article is retained as an additional linked source rather
    than a separate digest slot)."""
    labels: list[tuple[str, str]] = []
    primary_src = _primary_source(story.primary)
    if primary_src:
        labels.append((primary_src.get("publisher") or "Source", primary_src["url"]))
    if story.secondary:
        secondary_src = _primary_source(story.secondary)
        if secondary_src and secondary_src["url"] != (primary_src or {}).get("url"):
            labels.append((secondary_src.get("publisher") or "Source", secondary_src["url"]))
    return labels[:2]


def _source_line(story: Story) -> str | None:
    labels = _source_labels(story)
    if not labels:
        return None
    links = [_link(name, url) for name, url in labels]
    return "Source: " + ", ".join(links)


def _render_top_story(index: int, story: Story) -> list[str]:
    primary_src = _primary_source(story.primary)
    title_url = primary_src["url"] if primary_src else None
    lines = [
        "",
        f"{index}. {_link(story.primary.get('title', '(untitled)'), title_url)}",
        f"Category: {_escape(story.category)}",
    ]
    summary = summarize(story.primary.get("excerpt"), story.primary.get("title", ""))
    if summary:
        lines.append(_escape(summary))
    if story.update_note:
        lines.append(_escape(story.update_note))
    source_line = _source_line(story)
    if source_line:
        lines.append(source_line)
    return lines


def _render_more_to_know(story: Story) -> str:
    primary_src = _primary_source(story.primary)
    title_url = primary_src["url"] if primary_src else None
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

    if calendar_items:
        lines.append("")
        lines.append("TOMORROW TO WATCH")
        for cal in calendar_items:
            title = cal.get("title", "")
            url = cal.get("url")
            lines.append(f"• {_link(title, url) if url else _escape(title)}")

    return "\n".join(lines)
