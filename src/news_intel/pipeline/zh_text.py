"""Chinese-aware text processing (2026-08-17, zh_cn edition).

Simplified Chinese has no whitespace between words and uses its own
sentence-ending punctuation (。！？；), so the English pipeline's \\b-based
word-boundary matching and ASCII sentence-splitting (pipeline/summary_quality
._SENTENCE_END_RE, pipeline/citation.py's preamble regexes) do not apply.
pipeline/relevance.py's own term matcher already treats a non-ASCII
(topics_zh.yaml) term as a plain substring match rather than a \\b regex --
this module provides the remaining Chinese-specific primitives: sentence
splitting, aggregator/byline preamble stripping, and 万/亿/万亿-aware
numeric-magnitude parsing, so the zh_cn summary/render-readiness path never
silently reuses an English-only assumption.
"""

from __future__ import annotations

import re

# Chinese sentence-ending punctuation. Half-width "!"/"?"/";" are also
# accepted (mixed-width punctuation is common in Chinese tech writing --
# see the ithome/qbitai live samples, which freely mix full- and
# half-width characters).
_ZH_SENTENCE_END_RE = re.compile(r"[。！？；!?;](?=[^”’\"')\]]|$)")
_ZH_ELLIPSIS_SUFFIXES = ("……", "...", "…")

# Known structured preambles on Chinese titles/excerpts: aggregator
# citation cues (来源：/转自：), byline/editor cues (记者：/编辑：/撰文：),
# and "original title" cues (原标题：) that Chinese aggregators/portals
# commonly prepend -- general patterns, never a single-outlet special case.
_ZH_PREAMBLE_RE = re.compile(r"^\s*(来源|转自|原标题|记者|编辑|撰文|作者)\s*[：:]\s*")

# 万 (10^4), 亿 (10^8), 万亿 (10^12) -- checked longest-prefix-first so
# "万亿" is never mis-parsed as a "万" immediately followed by a stray "亿".
_ZH_UNIT_MULTIPLIERS: list[tuple[str, float]] = [
    ("万亿", 1_000_000_000_000.0),
    ("亿元", 100_000_000.0),
    ("亿美元", 100_000_000.0),
    ("亿", 100_000_000.0),
    ("万元", 10_000.0),
    ("万美元", 10_000.0),
    ("万", 10_000.0),
]

_ZH_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万亿|亿元|亿美元|亿|万元|万美元|万)")
_ZH_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:个百分点|%|％)")


def strip_zh_preamble(text: str) -> str:
    """Drops a leading structured preamble ("来源：新华社" / "记者：张三" /
    "原标题：...") from a title or excerpt. Never touches text without a
    recognized preamble -- a title that merely contains an early full-width
    colon for other reasons (e.g. a genuine "专题：..." headline convention)
    is left alone, since only the specific labelled-role words above are
    matched, not an arbitrary "X：" prefix."""
    if not text:
        return text
    match = _ZH_PREAMBLE_RE.match(text)
    if not match:
        return text
    rest = text[match.end() :].strip()
    return rest or text


def _strip_trailing_zh_ellipsis(text: str) -> str:
    stripped = text
    changed = True
    while changed:
        changed = False
        for suffix in _ZH_ELLIPSIS_SUFFIXES:
            rs = stripped.rstrip()
            if rs.endswith(suffix):
                stripped = rs[: -len(suffix)]
                changed = True
    return stripped


def zh_sentences(text: str, max_chars: int = 400) -> list[str]:
    """Complete, punctuation-terminated Chinese sentences from `text`
    (preamble-stripped, truncated to max_chars, trailing ellipsis
    dropped) -- mirrors pipeline/summary_quality._complete_sentences but
    using Chinese sentence-ending punctuation instead of the English
    [.!?] regex."""
    if not text:
        return []
    stripped = strip_zh_preamble(text)
    window = _strip_trailing_zh_ellipsis(stripped[:max_chars])
    matches = list(_ZH_SENTENCE_END_RE.finditer(window))
    sentences = []
    start = 0
    for m in matches:
        s = window[start : m.end()].strip()
        if s:
            sentences.append(s)
        start = m.end()
    return sentences


def parse_zh_amounts(text: str) -> list[float]:
    """Numeric magnitudes with 万/亿/万亿-style Chinese unit suffixes,
    converted to their absolute value (e.g. "115亿美元" -> 1.15e10). Does
    NOT parse bare Arabic-numeral dollar figures ("$136M") -- those already
    go through materiality._parse_dollar_values, which zh_cn text may also
    contain when quoting a USD figure verbatim (see ithome/qbitai samples,
    which routinely mix "1398 元" and Chinese-unit amounts in the same
    story)."""
    values: list[float] = []
    for amount_str, unit in _ZH_NUMBER_RE.findall(text):
        try:
            amount = float(amount_str)
        except ValueError:
            continue
        multiplier = next((m for suffix, m in _ZH_UNIT_MULTIPLIERS if suffix == unit), 1.0)
        values.append(amount * multiplier)
    return values


def format_zh_amount(value: float) -> str:
    """Renders a parsed magnitude back into readable 万/亿/万亿 form."""
    for threshold, suffix in ((1_000_000_000_000.0, "万亿"), (100_000_000.0, "亿"), (10_000.0, "万")):
        if value >= threshold:
            scaled = value / threshold
            number = f"{scaled:.1f}".removesuffix(".0")
            return f"{number}{suffix}"
    return f"{value:,.0f}"


def has_zh_text(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in (text or ""))
