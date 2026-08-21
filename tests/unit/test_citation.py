"""2026-08-13 repair: Techmeme's "Author / Publication: <headline> —
<sentence>" citation format is parsed for both provenance (real publisher
name, not "Techmeme") and summarization (the actual sentence, not the
restated headline that was eating the character window)."""

from __future__ import annotations

from news_intel.pipeline.citation import (
    resolve_citation_publisher,
    strip_citation_preamble,
    strip_title_preamble,
    strip_title_suffix,
)

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


# --- title preamble stripping (2026-08-16 title-normalization repair) ------


def test_strip_title_preamble_removes_sources_prefix():
    """Exact 2026-08-15 regression: 'Sources:' is Techmeme's own
    aggregation cue, never part of the original article's headline."""
    title = (
        "Sources: China plans to soon lift a travel ban on Manus founders as the company unwinds "
        "its $2B acquisition by Meta; CEO Xiao Hong plans to return to Singapore (Zijing Wu/Financial Times)"
    )
    result = strip_title_preamble(title)
    assert result.startswith("China plans to soon lift a travel ban")
    assert not result.startswith("Sources:")


def test_strip_title_preamble_removes_source_prefix():
    assert strip_title_preamble("Source: NVIDIA ships new chip") == "NVIDIA ships new chip"


def test_strip_title_preamble_removes_more_prefix():
    assert strip_title_preamble("More: details emerge on the Anthropic deal") == "details emerge on the Anthropic deal"


def test_strip_title_preamble_removes_author_publication_prefix():
    title = "Jing Yang / The Information: Manus returns to independent operation"
    assert strip_title_preamble(title) == "Manus returns to independent operation"


def test_strip_title_preamble_leaves_already_clean_title_unchanged():
    title = (
        "Point2 Technology, which develops RF-based data center interconnection tech, raised a "
        "$136M Series B from LB Investment, Arm, Maverick Silicon, and others (Giacomo Lee/SDxCentral)"
    )
    assert strip_title_preamble(title) == title


def test_strip_title_preamble_does_not_strip_a_legitimate_headline_colon():
    """A genuine headline that happens to start with a word-then-colon
    construction (not a recognized aggregator cue) must be left alone --
    no FT-/Nvidia-specific exceptions, just a general known-word/citation-
    pattern allowlist."""
    assert strip_title_preamble("Analysis: Why NVIDIA's guidance disappointed investors") == (
        "Analysis: Why NVIDIA's guidance disappointed investors"
    )
    assert strip_title_preamble("NVIDIA Q3: record revenue beats estimates") == (
        "NVIDIA Q3: record revenue beats estimates"
    )


def test_strip_title_preamble_handles_empty_and_none_gracefully():
    assert strip_title_preamble("") == ""


# 2026-08-21 repair: real defect (Aug 20 en_global digest, already
# delivered) -- "Supermicro investigation clears CEO in $2.5 billion
# alleged smuggling scheme | Fortune" was rendered with the site's own
# "| Fortune" title suffix still attached.


def test_strip_title_suffix_drops_trailing_publisher_pipe():
    title = "Supermicro investigation clears CEO in $2.5 billion alleged smuggling scheme | Fortune"
    assert strip_title_suffix(title) == "Supermicro investigation clears CEO in $2.5 billion alleged smuggling scheme"


def test_strip_title_suffix_leaves_a_title_with_no_pipe_unchanged():
    title = "A title with no suffix at all"
    assert strip_title_suffix(title) == title


def test_strip_title_suffix_leaves_empty_string_unchanged():
    assert strip_title_suffix("") == ""


def test_strip_title_suffix_never_returns_empty_for_a_pipe_only_title():
    """Never discards content it can't confidently identify as more than
    a bare suffix -- falls back to the original text unchanged."""
    result = strip_title_suffix("| Fortune")
    assert result  # non-empty
