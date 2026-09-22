"""Splits long digest text into Telegram-safe chunks at section (blank-line)
boundaries, never mid-sentence, staying under the hard Telegram message
limit.

2026-09-22: HTML-safe. Messages are Telegram HTML (delivery/telegram_html.py),
so a cut must never fall inside a tag, inside an <a>...</a> element, or
inside an entity -- Telegram rejects the part, or shows markup literally.
Cut order: blank line -> line break -> whitespace between complete
elements. Only if a single element is itself longer than the limit is its
link markup dropped (its escaped text kept) so it can be cut at whitespace.
Nothing is ever escaped or unescaped here.
"""

from __future__ import annotations

import re

from news_intel.delivery.telegram_html import escape_text, html_to_plain

_ELEMENT_RE = re.compile(r"<([A-Za-z][A-Za-z0-9-]*)\b[^>]*>.*?</\1>", re.DOTALL)
_ENTITY_TAIL_RE = re.compile(r"&[#A-Za-z0-9]{0,9}$")


def _atoms(line: str) -> list[str]:
    """Complete elements and whitespace-separated text runs, in order,
    each followed by the whitespace that came after it."""
    atoms: list[str] = []
    pos = 0
    for m in _ELEMENT_RE.finditer(line):
        atoms.extend(re.findall(r"\S+\s*|\s+", line[pos : m.start()]))
        tail = re.match(r"\s*", line[m.end():]).group(0)
        atoms.append(m.group(0) + tail)
        pos = m.end() + len(tail)
    atoms.extend(re.findall(r"\S+\s*|\s+", line[pos:]))
    return atoms


def _hard_split_text(text: str, max_chars: int) -> list[str]:
    """Escaped plain text with no markup: cut at whitespace, else at a
    character boundary that is not inside an entity."""
    out = []
    while len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
            m = _ENTITY_TAIL_RE.search(text[:cut])
            if m:
                cut = m.start()
        out.append(text[:cut])
        text = text[cut:].lstrip(" ")
    if text:
        out.append(text)
    return out


def _split_line(line: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for atom in _atoms(line):
        if len(atom) > max_chars:
            if current:
                parts.append(current.rstrip())
                current = ""
            # a single element longer than a whole message: keep its text,
            # drop its link markup, cut the escaped text safely
            parts.extend(_hard_split_text(escape_text(html_to_plain(atom)), max_chars))
            continue
        if len(current) + len(atom.rstrip()) > max_chars:
            parts.append(current.rstrip())
            current = atom
        else:
            current += atom
    if current.strip():
        parts.append(current.rstrip())
    return parts


def _pack(pieces: list[str], sep: str, max_chars: int, splitter) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        for sub in ([piece] if len(piece) <= max_chars else splitter(piece, max_chars)):
            candidate = f"{current}{sep}{sub}" if current else sub
            if len(candidate) > max_chars and current:
                chunks.append(current)
                current = sub
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def _split_block(block: str, max_chars: int) -> list[str]:
    return _pack(block.split("\n"), "\n", max_chars, _split_line)


def split_at_sections(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    return _pack(text.split("\n\n"), "\n\n", max_chars, _split_block)


def label_parts(chunks: list[str]) -> list[str]:
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [f"{chunk}\n\n({i}/{total})" for i, chunk in enumerate(chunks, start=1)]


_LABEL_RESERVE_CHARS = 16


def split_and_label(text: str, max_chars: int) -> list[str]:
    """Split AND label while guaranteeing every returned chunk -- label
    included -- is at most `max_chars`.

    2026-08-29: label_parts() appends "\\n\\n(i/N)" AFTER splitting, so the
    label's cost is reserved before splitting. 2026-09-22: the old final
    `chunk[:max_chars]` truncation could cut through markup; the limit now
    holds by construction and is asserted instead."""
    if len(text) <= max_chars:
        return [text]
    chunks = label_parts(split_at_sections(text, max_chars - _LABEL_RESERVE_CHARS))
    assert all(len(c) <= max_chars for c in chunks), "splitter exceeded the message limit"
    return chunks
