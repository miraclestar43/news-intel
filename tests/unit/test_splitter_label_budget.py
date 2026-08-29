"""2026-08-29: label_parts() appends "\\n\\n(i/N)" AFTER split_at_sections
has already packed each chunk up to max_chars, so a chunk produced at
exactly the limit came back 7 characters over it and Telegram would reject
the send. split_at_sections' hard-wrap branch produces exactly-max_chars
chunks whenever a single block exceeds the limit, so this was reachable,
not theoretical."""

from __future__ import annotations

import pytest

from news_intel.delivery.splitter import label_parts, split_and_label, split_at_sections

LIMIT = 4096


def test_the_original_defect_is_real():
    """Pins WHY split_and_label exists: the naive composition overflows."""
    chunks = split_at_sections("y" * 9000, LIMIT)
    assert max(len(c) for c in chunks) == LIMIT  # hard-wrap lands exactly on it
    naive = label_parts(chunks)
    assert max(len(c) for c in naive) > LIMIT


@pytest.mark.parametrize(
    "text",
    [
        "y" * 9000,                                        # one oversized block -> hard-wrap path
        "\n\n".join(f"BLOCK{i:02d} " + "x" * 300 for i in range(30)),  # normal section path
        "\n\n".join("B" * 4095 for _ in range(3)),         # blocks at the boundary
        "z" * (LIMIT + 1),                                 # one char over
        "z" * LIMIT,                                       # exactly at the limit
    ],
)
def test_every_labeled_chunk_fits_the_limit(text):
    for chunk in split_and_label(text, LIMIT):
        assert len(chunk) <= LIMIT, f"chunk of {len(chunk)} chars exceeds {LIMIT}"


def test_single_message_is_returned_unlabeled():
    text = "short digest"
    assert split_and_label(text, LIMIT) == [text]


def test_multi_part_output_is_labeled_and_ordered():
    parts = split_and_label("\n\n".join("B" * 2000 for _ in range(6)), LIMIT)
    assert len(parts) > 1
    total = len(parts)
    for i, part in enumerate(parts, start=1):
        assert part.endswith(f"({i}/{total})")


def test_no_content_is_lost_when_splitting_at_section_boundaries():
    blocks = [f"BLOCK{i:02d} " + "x" * 300 for i in range(30)]
    parts = split_and_label("\n\n".join(blocks), LIMIT)
    joined = "".join(parts)
    for block in blocks:
        assert block.split()[0] in joined
