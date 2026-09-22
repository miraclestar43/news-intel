"""The ONE place Telegram HTML is produced and checked (2026-09-22 incident).

Rendering contract:
- Every stored or intermediate text field (headline, summary, category,
  source name, LLM prose) is canonical plain Unicode: no entities, no
  presentation markup (pipeline/text.py).
- Telegram HTML is built only at the output boundary: text is escaped
  exactly once (escape_text), links are built with link(), and nothing
  already-HTML is ever escaped again.
- Every Telegram HTML message is sent with parse_mode="HTML" (PARSE_MODE),
  on every path and for every split part. A plain body is converted with
  plain_to_html() -- escaped once -- never sent in a different mode.
- CLI/plain output uses html_to_plain(): no tags, no entities.

Sep 21 incident: the renderer's HTML was correct, but run_daily sent it
with parse_mode=None whenever a story had a validated LLM editorial result
(the mode was keyed on `fallback_used`), so Telegram showed `<a href=...>`,
`AMD&#x27;s`, `AI &amp; Semiconductors` literally -- every [GLOBAL DAILY]
since Sep 1 and [中文日报] from Sep 21.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

PARSE_MODE = "HTML"
# Telegram Bot API HTML subset
ALLOWED_TAGS = frozenset({"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
                          "tg-spoiler", "span", "blockquote"})
_BARE_AMP_RE = re.compile(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|amp|lt|gt|quot);)")


class TelegramHTMLError(ValueError):
    pass


def escape_text(text: str | None) -> str:
    """Canonical plain text -> Telegram HTML text. Exactly once."""
    return html.escape(text or "", quote=True)


def link(text: str | None, url: str | None) -> str:
    """<a> with the text and the URL each escaped exactly once (a URL's own
    '&' becomes '&amp;' inside the attribute, as HTML requires)."""
    if not url:
        return escape_text(text)
    return f'<a href="{escape_text(url)}">{escape_text(text)}</a>'


def plain_to_html(text: str | None) -> str:
    return escape_text(text)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.parts.append(data)


def _parse(text: str) -> _Parser:
    p = _Parser()
    p.feed(text)
    p.close()
    if p.stack:
        p.errors.append(f"unclosed <{p.stack[-1]}>")
    return p


def validate(text: str) -> None:
    """Raises TelegramHTMLError for anything Telegram's HTML parser would
    reject: unsupported/unbalanced tags, a bare '&' or a truncated tag."""
    if _BARE_AMP_RE.search(text):
        raise TelegramHTMLError("bare '&' (not an entity)")
    # Python's parser silently treats these as text; Telegram rejects them
    if re.search(r"<(?!/?[A-Za-z])", text):
        raise TelegramHTMLError("bare '<' (not a tag)")
    if re.search(r"<[^>]*$", text):
        raise TelegramHTMLError("truncated tag")
    errors = _parse(text).errors
    if errors:
        raise TelegramHTMLError("; ".join(errors))


def html_to_plain(text: str) -> str:
    """Telegram HTML -> what a reader sees, as plain text (link text kept,
    tags dropped, entities decoded once)."""
    return "".join(_parse(text).parts)
