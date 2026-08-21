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
from decimal import Decimal

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

# 2026-08-21 repair: a real fabricated-number defect -- a "相关阅读"
# (related-reading) trailer appended to the end of an article is a link
# list to OTHER, separate stories, not part of the article itself. A TCL/
# 广州华星 story's trailer contained an unrelated LG Display/TCL
# transaction ("LG 显示以定价 2.2466 万亿韩元 将广州 LCD 工厂出售给 TCL 华星"),
# and parse_zh_amounts (before this fix) treated that trailer's 2.2466万亿
# figure as if it were part of THIS story, fabricating a why-it-matters
# amount ~236x larger than the story's real 93.25亿元 transaction value.
# General fix, not story-specific: strip the trailer before any
# extraction -- numbers, entities, and sentences alike should never be
# drawn from a block of unrelated "you might also like" links.
_ZH_TRAILER_RE = re.compile(r"(相关阅读|延伸阅读|推荐阅读|猜你喜欢|更多阅读)\s*[:：]?", re.DOTALL)

# AI-generated "reading companion" boilerplate some sources append (e.g.
# ithome's own "AI 解读" note) -- editorial commentary ABOUT the article,
# not the article's own reported facts; stripped for the same reason as
# the related-reading trailer.
_ZH_AI_COMMENTARY_RE = re.compile(r"AI\s*解读\s*")


def strip_zh_trailer(text: str) -> str:
    """Drops a trailing "相关阅读：..."/"延伸阅读：..." related-link block
    and any "AI 解读" commentary note -- both are aggregator furniture
    appended after the real article content, describing OTHER stories or
    commenting ABOUT this one, never additional facts of the story itself.
    Never touches text with no recognized trailer marker."""
    if not text:
        return text
    match = _ZH_TRAILER_RE.search(text)
    if match:
        text = text[: match.start()].rstrip()
    match = _ZH_AI_COMMENTARY_RE.search(text)
    if match:
        text = text[: match.start()].rstrip()
    return text


# 万 (10^4), 亿 (10^8), 万亿 (10^12) -- checked longest-prefix-first so
# "万亿" is never mis-parsed as a "万" immediately followed by a stray "亿".
# Decimal (not float) multipliers -- combined with parsing the digit
# string through Decimal in parse_zh_amounts, this keeps the conversion
# exact (93.25亿元 -> exactly 9,325,000,000, never a binary-float rounding
# artifact) end to end, not just "close enough".
_ZH_UNIT_MULTIPLIERS: list[tuple[str, Decimal]] = [
    ("万亿", Decimal("1000000000000")),
    ("亿元", Decimal("100000000")),
    ("亿美元", Decimal("100000000")),
    ("亿", Decimal("100000000")),
    ("万元", Decimal("10000")),
    ("万美元", Decimal("10000")),
    ("万", Decimal("10000")),
]

# 2026-08-21 repair: a real fabricated-number defect (see _ZH_TRAILER_RE's
# docstring) surfaced a second, independent bug in the same story -- even
# without the trailer, "2.2466万亿韩元" (2.2466 TRILLION KOREAN WON, a
# different currency entirely) would have matched this regex's "万亿"
# alternative and been silently treated as if it were a same-magnitude
# yuan amount, with "韩元" simply left unconsumed and the currency
# distinction lost entirely. The negative lookahead below refuses to match
# a Chinese-unit amount immediately followed by a foreign-currency marker
# -- general currency-awareness, not specific to this one story or to won.
#
# Applied as a POST-MATCH filter (checked against the text immediately
# after a full match), not an inline regex lookahead: an inline lookahead
# on the "万亿" alternative alone would only block THAT alternative, and
# Python's alternation then falls through to try the next-shorter
# alternative ("万") at the same position, which still matches and still
# fabricates a number (just a different wrong one -- "2.2466万亿韩元"
# would silently become "2.2466万" instead of being excluded outright).
# Filtering after a single greedy (longest-alternative-first) match avoids
# that fallthrough entirely.
_FOREIGN_CURRENCY_MARKERS = (
    "韩元", "日元", "欧元", "英镑", "卢比", "泰铢", "越南盾", "港元", "港币", "新台币", "新元", "卢布", "里拉",
)
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
    story). Strips 相关阅读/AI 解读 aggregator furniture first (see
    strip_zh_trailer) so an unrelated linked story's figures, possibly in
    an entirely different currency, are never picked up as this story's
    own. Decimal-based conversion keeps the result exact."""
    text = strip_zh_trailer(text)
    values: list[float] = []
    for match in _ZH_NUMBER_RE.finditer(text):
        amount_str, unit = match.group(1), match.group(2)
        tail = text[match.end() : match.end() + 4]
        if any(tail.startswith(marker) for marker in _FOREIGN_CURRENCY_MARKERS):
            continue
        try:
            amount = Decimal(amount_str)
        except (ValueError, ArithmeticError):
            continue
        multiplier = next((m for suffix, m in _ZH_UNIT_MULTIPLIERS if suffix == unit), Decimal(1))
        values.append(float(amount * multiplier))
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
