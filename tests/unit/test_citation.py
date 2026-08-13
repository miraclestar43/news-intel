"""2026-08-13 repair: Techmeme's "Author / Publication: <headline> —
<sentence>" citation format is parsed for both provenance (real publisher
name, not "Techmeme") and summarization (the actual sentence, not the
restated headline that was eating the character window)."""

from __future__ import annotations

from news_intel.pipeline.citation import resolve_citation_publisher, strip_citation_preamble

_RUNTIMEWIRE_EXCERPT = (
    "Kate Knibbs / Wired : A look at AI news website RuntimeWire, which costs $100/day to run "
    "and \"beat\" human reporters to OpenAI's Black Hat disclosure, favoring quantity over quality "
    "—\xa0 Last week, an AI newsroom beat mainstream journalists—including WIRED—to a story about "
    "OpenAI and hacking.\xa0 It's just the beginning."
)
_DECART_EXCERPT = (
    "Bloomberg : Sources: Anthropic is in talks to buy Decart, which offers real-time generative "
    "video and GPU optimization tech, for about $6B —\xa0 Anthropic PBC is in talks to buy the "
    "artificial intelligence startup Decart AI for about $6 billion, according to people familiar "
    "with the matter."
)


def test_resolves_author_slash_publication_format():
    assert resolve_citation_publisher(_RUNTIMEWIRE_EXCERPT) == "Wired"


def test_resolves_bare_publication_format():
    assert resolve_citation_publisher(_DECART_EXCERPT) == "Bloomberg"


def test_no_prefix_returns_none():
    assert resolve_citation_publisher("Nebius Group NV reported a 514% jump in second-quarter sales.") is None


def test_none_excerpt_returns_none():
    assert resolve_citation_publisher(None) is None


def test_strip_preamble_exposes_the_actual_sentence():
    remainder = strip_citation_preamble(_RUNTIMEWIRE_EXCERPT)
    assert remainder.startswith("Last week, an AI newsroom beat")
    assert "RuntimeWire, which costs" not in remainder  # restated headline is gone


def test_strip_preamble_falls_back_to_original_when_no_dash_structure():
    text = "Just a plain excerpt with no citation prefix at all."
    assert strip_citation_preamble(text) == text
