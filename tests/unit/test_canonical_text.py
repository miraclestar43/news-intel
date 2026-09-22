"""Canonical text: markup removed, entities decoded exactly once (2026-09-22)."""

import pytest

from news_intel.pipeline.text import canonical_text


@pytest.mark.parametrize("raw,canon", [
    ("Q&amp;A: AMD&#x27;s <b>plan</b>", "Q&A: AMD's plan"),
    ("AI &amp; Semiconductors", "AI & Semiconductors"),
    ("Why &lt;AI&gt; matters", "Why <AI> matters"),            # escaped text stays text
    ("英伟达 & AMD：<AI> 需求强劲", "英伟达 & AMD：<AI> 需求强劲"),  # already-decoded text is untouched
    ("<p>First<br/>second</p>", "First second"),
    ("x&nbsp;y  z", "x y z"),
    ("AMD's market cap & AI demand", "AMD's market cap & AI demand"),   # canonical input is a fixed point
])
def test_canonical_text(raw, canon):
    assert canonical_text(raw) == canon
    assert canonical_text(canon) == canon
