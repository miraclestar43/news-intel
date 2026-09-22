"""Canonical text (2026-09-22 rendering contract, see
delivery/telegram_html.py): every stored or intermediate text field is
plain Unicode -- presentation markup removed, HTML entities decoded
exactly once, whitespace (incl. &nbsp;) collapsed.

Applied at normalization (new rows) and on read (legacy rows are never
rewritten: a few titles were stored as "Q&amp;A").
"""

from __future__ import annotations

import html
import re

# Only REAL HTML element names are markup. Feeds often deliver already-
# decoded text, so a headline may legitimately contain "<AI>" -- that is
# text and must survive (it is escaped once at the output boundary).
_HTML_ELEMENTS = ("a|abbr|b|big|blockquote|br|cite|code|del|dfn|div|em|figure|figcaption|font|h[1-6]|hr|i|img|"
                  "ins|kbd|li|mark|ol|p|pre|q|s|samp|small|span|strike|strong|sub|sup|table|tbody|td|th|thead|"
                  "tr|tt|u|ul|var|wbr|iframe|script|style|picture|source|video|audio|time")
_TAG_RE = re.compile(rf"</?(?:{_HTML_ELEMENTS})\b(?:\s[^<>]*)?/?>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t ​]+")


def canonical_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = _TAG_RE.sub(" ", value)   # "<p>a<br/>b</p>" -> "a b", never "ab"
    text = html.unescape(text)          # exactly once
    text = _WS_RE.sub(" ", text)
    return text.strip()
