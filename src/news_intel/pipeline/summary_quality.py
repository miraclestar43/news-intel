"""Shared summary-extraction logic (2026-08-13 repair): used both by the
renderer (delivery/daily_production_formatter.py) to produce the displayed
summary line, and by pipeline/daily_selection.py to decide whether a
candidate has enough source-supported information to occupy a TOP slot at
all. Split out from the formatter specifically so daily_selection.py can
import it without a circular dependency (the formatter already imports
Story/SelectionResult from daily_selection).

A fallback summary is used only if it is a complete, source-supported
sentence that does not end in an unresolved ellipsis and is not essentially
a restatement of the title; otherwise there is no summary. Never fabricates
or extrapolates missing facts.
"""

from __future__ import annotations

import re

from news_intel.pipeline.citation import strip_citation_preamble
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
    # 2026-08-13 fix: a Techmeme-style "Publication: <restated headline> —"
    # citation preamble was eating the character window before the actual
    # new sentence ever appeared, so every Techmeme-sourced summary came
    # back empty. Stripped here (not at the source) so provenance-parsing
    # (event_repo._display_publisher) still sees the original excerpt.
    excerpt = strip_citation_preamble(excerpt)
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


def extract_summary(excerpt: str | None, title: str, *, max_chars: int = SUMMARY_MAX_CHARS) -> str | None:
    """None means: no valid summary is available -- never a truncated
    fragment, never a restatement of the title, never an invented fact."""
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


def has_complete_summary(item: dict) -> bool:
    """Used by daily_selection.py: does this candidate have enough stored,
    source-supported text to explain what happened in at least one
    complete sentence? A headline alone is not enough for a TOP slot."""
    return extract_summary(item.get("excerpt"), item.get("title", "")) is not None


def best_available_summary(primary: dict, secondary: dict | None) -> str | None:
    """Tries the primary source's excerpt first, then the secondary's
    (2026-08-14: "attempt to resolve and summarize the higher-value event
    from its valid original source first" -- a family whose highest-ranked
    member has a poorly-formatted excerpt but whose linked secondary
    source has a clean one should not lose its summary over that). Shared
    by delivery/daily_production_formatter.py's rendered "what happened"
    line and pipeline/render_readiness.py's TOP render-readiness gate, so
    both agree on exactly what counts as "enrichment was attempted"."""
    title = primary.get("title", "")
    summary = extract_summary(primary.get("excerpt"), title)
    if summary:
        return summary
    if secondary:
        return extract_summary(secondary.get("excerpt"), title)
    return None
