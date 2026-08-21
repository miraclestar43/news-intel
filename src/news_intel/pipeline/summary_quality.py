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
_FALLBACK_MIN_CHARS = 30
# A truncation that cuts off immediately after a bare dollar figure is
# categorically misleading, not just inelegant -- "$500" could be $500,
# $500 million, or $500 billion, and the missing unit is exactly the part
# that got cut. This is the original 2026-08-10 defect the strict
# terminal-punctuation rule was built to catch; the fallback-clause
# relaxation below must still reject it even though it no longer requires
# terminal punctuation in general.
_DANGLING_DOLLAR_RE = re.compile(r"\$\s?[\d,]+\.?\d*$")

# 2026-08-21 repair: a real defect (Aug 20 production replay) -- "Broadcom
# Inc. is in talks with a group of lenders to raise more than $60 billion
# in debt ..." was truncated down to the bare fragment "Broadcom Inc." by
# _SENTENCE_END_RE treating the period in the corporate-suffix abbreviation
# "Inc." as a genuine sentence terminator. An entity name plus a business
# abbreviation is not itself a complete sentence -- general, not specific
# to Broadcom: any "<Name> Corp./Inc./Ltd./Co." pattern has the same issue.
_ABBREVIATIONS_NOT_SENTENCE_END = frozenset(
    {"inc", "corp", "co", "ltd", "llc", "plc", "vs", "etc", "mr", "mrs", "ms", "dr", "jr", "sr", "st", "u.s", "u.k"}
)
_WORD_BEFORE_PERIOD_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")


def _is_abbreviation_period(text_up_to_period: str) -> bool:
    match = _WORD_BEFORE_PERIOD_RE.search(text_up_to_period)
    if not match:
        return False
    word = match.group(1).rstrip(".").lower()
    return word in _ABBREVIATIONS_NOT_SENTENCE_END


# 2026-08-21 repair: a second real defect from the same replay -- "Super
# Micro Computer said on Thursday that an independent investigation led by
# its board found no evidence that current members ..." was accepted by
# the fallback-clause path (no real terminal punctuation, but long enough
# to clear the length floor) even though the trailing "that current
# members" clause never resolves -- the source excerpt itself was
# truncated mid-clause, and appending a period made an unfinished sentence
# LOOK grammatically complete. General (not headline-specific) heuristic:
# find the LAST subordinating conjunction near the end of the text; if
# the words after it are few and none look verb-like, the clause never
# reached a resolution.
_DANGLING_CONJUNCTIONS = (
    "that", "which", "who", "whose", "because", "since", "although", "though", "while", "if", "unless",
)
_COMMON_VERB_SUFFIXES = ("ed", "ing")
_COMMON_BARE_VERBS = frozenset(
    {
        "is", "are", "was", "were", "be", "been", "has", "have", "had", "will", "would", "could", "should",
        "can", "may", "might", "do", "does", "did", "said", "says", "claims", "denies", "confirms", "plans",
        "expects", "believes", "knew", "know", "knows", "found", "find", "finds", "aware", "involved", "acted",
    }
)
_DANGLING_CLAUSE_MAX_TAIL_WORDS = 6


def _ends_with_dangling_clause(text: str) -> bool:
    """True if the text's last subordinate clause (introduced by "that"/
    "which"/... near the end) never reaches a verb -- a strong signal the
    source excerpt was truncated mid-thought, not that this is a genuine,
    if unusual, complete sentence."""
    lowered = text.lower()
    last_idx = -1
    last_conj_len = 0
    for conj in _DANGLING_CONJUNCTIONS:
        idx = lowered.rfind(f" {conj} ")
        if idx > last_idx:
            last_idx = idx
            last_conj_len = len(conj) + 2
    if last_idx == -1:
        return False
    tail = text[last_idx + last_conj_len :].strip().rstrip(".!?")
    tail_words = tail.split()
    if not tail_words or len(tail_words) > _DANGLING_CLAUSE_MAX_TAIL_WORDS:
        return False
    for word in tail_words:
        cleaned = word.strip(",.\"'").lower()
        if cleaned in _COMMON_BARE_VERBS:
            return False
        if len(cleaned) > 3 and any(cleaned.endswith(suf) for suf in _COMMON_VERB_SUFFIXES):
            return False
    return True


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
    sentences = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(window):
        if window[m.start()] == "." and _is_abbreviation_period(window[: m.start() + 1]):
            continue
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
    if sentences:
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
    return _fallback_clause(excerpt, title, max_chars)


def _fallback_clause(excerpt: str, title: str, max_chars: int) -> str | None:
    """2026-08-16 render-readiness repair: real Aug 15 production
    candidates routinely have a genuine, substantive clause with no
    terminal [.!?] at all (Techmeme's own excerpt formatting, e.g. "...
    nears resolution of Meta separation" with no trailing period) or one
    that trails off mid-clause behind an explicit "..."/"…" truncation
    marker (e.g. "... a massive planned Ohio …"). The strict sentence-
    punctuation rule above treated both as "no summary", which left every
    TOP slot unfillable and forced an empty TOP section even though real,
    unaltered source text was available. This uses that same stripped-
    preamble text verbatim -- dropping only the explicit ellipsis
    truncation marker itself and appending a period for readability -- a
    formatting decision, never an invented word. A short/near-empty
    remainder (e.g. the excerpt is nothing but the citation preamble)
    still yields no summary, and it is still checked for redundancy with
    the title exactly like a normal sentence would be."""
    fallback = strip_citation_preamble(excerpt)
    fallback = _strip_trailing_ellipsis(fallback[:max_chars]).strip()
    if len(fallback) < _FALLBACK_MIN_CHARS or _is_redundant_with_title(fallback, title):
        return None
    if _DANGLING_DOLLAR_RE.search(fallback):
        return None
    if _ends_with_dangling_clause(fallback):
        return None
    return fallback if fallback[-1] in ".!?" else fallback + "."


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
