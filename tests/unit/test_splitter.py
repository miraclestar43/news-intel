from news_intel.delivery.splitter import label_parts, split_at_sections


def test_short_text_not_split():
    text = "para one\n\npara two"
    assert split_at_sections(text, max_chars=1000) == [text]


def test_splits_at_blank_line_boundaries():
    blocks = ["Header"] + [f"Section {i}\nsome body text here" for i in range(10)]
    text = "\n\n".join(blocks)
    chunks = split_at_sections(text, max_chars=80)
    assert len(chunks) > 1
    assert all(len(c) <= 80 for c in chunks)
    # no section text lost
    rejoined = "\n\n".join(chunks)
    for b in blocks:
        assert b in rejoined


def test_label_parts_adds_counters():
    chunks = ["a", "b", "c"]
    labeled = label_parts(chunks)
    assert labeled[0].endswith("(1/3)")
    assert labeled[-1].endswith("(3/3)")


def test_label_parts_noop_for_single_chunk():
    assert label_parts(["only one"]) == ["only one"]
