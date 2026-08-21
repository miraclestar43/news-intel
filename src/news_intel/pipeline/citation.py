"""Techmeme-style citation-prefix parsing (2026-08-13 repair).

Techmeme excerpts are consistently formatted as either:
  "Author Name / Publication: <restated headline> — <actual sentence>"
  "Publication: <restated headline> — <actual sentence>"

Two independent problems this fixes:
  1. Provenance (event_repo.py): the displayed "Source:" label said
     "Techmeme" for every Techmeme-discovered item, even though the real
     original publisher (Wired, Bloomberg, ...) is right there in the
     excerpt's own citation prefix. Techmeme's canonical_url still points
     at Techmeme's own roundup page (unlike GDELT, there is no separate
     original-article URL to link to), so only the *label* changes here.
  2. Summarization (delivery/daily_production_formatter.py): the citation
     prefix restates the (often very long) headline before the actual new
     sentence, which pushed the real sentence past the summary module's
     character window entirely -- summarize() found zero complete
     sentences and every Techmeme-sourced TOP story rendered with no body
     text at all (2026-08-12 diagnosis).
"""

from __future__ import annotations

import re

_CITATION_PREFIX_RE = re.compile(r"^\s*([^:]{1,80}?)\s*:\s*")
_AUTHOR_PUB_RE = re.compile(r"^(.+?)\s*/\s*(.+)$")
_DASH_SPLIT_RE = re.compile(r"\s*[—–-]{1,2}\s+")

_MAX_PUBLISHER_NAME_LEN = 40


def resolve_citation_publisher(excerpt: str | None) -> str | None:
    """Returns the resolved original-publisher name (e.g. "Wired",
    "Bloomberg") from a Techmeme-style citation prefix, or None if the
    excerpt doesn't start with a recognizable "Name:" / "Name / Name:"
    prefix."""
    if not excerpt:
        return None
    match = _CITATION_PREFIX_RE.match(excerpt)
    if not match:
        return None
    prefix = match.group(1).strip()
    author_pub = _AUTHOR_PUB_RE.match(prefix)
    publisher = author_pub.group(2).strip() if author_pub else prefix
    if not publisher or len(publisher) > _MAX_PUBLISHER_NAME_LEN:
        return None
    return publisher


# 2026-08-16 title-normalization repair: "Sources: China plans..." and
# "Sources: Nvidia in talks..." were delivered verbatim as the rendered
# TOP/MORE headline -- "Sources:" is Techmeme's own aggregation cue
# ("this is sourced reporting, not an official release"), never part of
# either original article's actual headline. General, not title-specific:
# recognizes the same bare-word aggregation cues Techmeme uses
# (Sources/Source/More) plus the "Author / Publication:" citation format
# already handled for excerpts above -- never strips an arbitrary
# "Word:" prefix that could be a legitimate part of a real headline
# (e.g. "Analysis: ..." or "NVIDIA Q3: record revenue" are left alone).
_KNOWN_TITLE_PREAMBLE_WORDS = frozenset({"sources", "source", "more"})


def strip_title_preamble(title: str) -> str:
    """Drops a recognized aggregator preamble ("Sources:", "Source:",
    "More:", or an "Author / Publication:" citation prefix) from the start
    of a title, returning the title unchanged if no recognized preamble is
    present -- never touches a title that merely happens to contain an
    early colon for other reasons."""
    if not title:
        return title
    match = _CITATION_PREFIX_RE.match(title)
    if not match:
        return title
    prefix = match.group(1).strip()
    is_known_word = prefix.lower() in _KNOWN_TITLE_PREAMBLE_WORDS
    is_author_pub = "/" in prefix
    if not (is_known_word or is_author_pub):
        return title
    rest = title[match.end() :].strip()
    return rest or title


# 2026-08-21 repair: real defect (Aug 20 en_global digest, already
# delivered) -- "Supermicro investigation clears CEO in $2.5 billion
# alleged smuggling scheme | Fortune" was rendered with the site's own
# "| Fortune" title suffix still attached, since the origin-fetched title
# is used verbatim. A trailing "| <short text>" is an extremely common
# website <title>-tag convention ("Article Headline | Site Name"), rarely
# genuine headline content -- general, not specific to Fortune. Scoped to
# the pipe character specifically (not hyphen/en-dash, which commonly
# appear inside legitimate headline clauses and would risk over-stripping
# real content).
_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*[^|]{1,40}$")


def strip_title_suffix(title: str) -> str:
    """Drops a trailing "| Publisher Name" suffix from a title, returning
    the title unchanged if no pipe-delimited suffix is present."""
    if not title or "|" not in title:
        return title
    match = _TITLE_SUFFIX_RE.search(title)
    if not match:
        return title
    rest = title[: match.start()].strip()
    return rest or title


def strip_citation_preamble(excerpt: str) -> str:
    """Drops a leading "Name: <restated headline> —" citation prefix
    so summarization windows over the actual new sentence instead of a
    restatement of the title. Falls back to the original text unchanged if
    no citation-prefix/dash structure is found (never discards content it
    can't confidently identify as a restated preamble)."""
    match = _CITATION_PREFIX_RE.match(excerpt)
    if not match:
        return excerpt
    after_colon = excerpt[match.end():]
    dash_match = _DASH_SPLIT_RE.search(after_colon)
    if not dash_match:
        return excerpt
    return after_colon[dash_match.end():]
