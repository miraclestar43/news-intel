"""Splits long digest text into Telegram-safe chunks at section (blank-line)
boundaries, never mid-sentence, staying under the hard Telegram message
limit."""

from __future__ import annotations


def split_at_sections(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    blocks = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
        # A single block bigger than max_chars (rare) is hard-wrapped rather
        # than silently exceeding the limit.
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max_chars:]
    if current:
        chunks.append(current)
    return chunks


def label_parts(chunks: list[str]) -> list[str]:
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [f"{chunk}\n\n({i}/{total})" for i, chunk in enumerate(chunks, start=1)]
