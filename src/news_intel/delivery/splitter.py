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


# Worst-case cost label_parts adds to a chunk: "\n\n" plus "(NN/NN)".
# Generous enough to cover a three-digit part count.
_LABEL_RESERVE_CHARS = 16


def split_and_label(text: str, max_chars: int) -> list[str]:
    """Split AND label while guaranteeing every returned chunk -- label
    included -- is at most `max_chars`.

    2026-08-29: label_parts() appends "\\n\\n(i/N)" AFTER splitting, so a
    chunk that split_at_sections produced at exactly max_chars (its
    hard-wrap branch does exactly that for an oversized single block) came
    back at max_chars + 7 and would be rejected by Telegram. Reserving the
    label's cost before splitting makes the limit hold by construction
    rather than by luck of where block boundaries happen to fall."""
    if len(text) <= max_chars:
        return [text]
    chunks = label_parts(split_at_sections(text, max_chars - _LABEL_RESERVE_CHARS))
    # Defensive: never hand the caller something the transport will reject.
    return [chunk[:max_chars] for chunk in chunks]
