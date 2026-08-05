"""Gate A remediation Phase 5 regression: pipeline/entities.py's known-
entity matching must use word/phrase boundaries, not bare substrings.
Discovered by replaying the Gate A snapshot through the corrected
pipeline: "Intel" matched inside "intelligence" (and, by the same class of
bug fixed for topic classification in pipeline/relevance.py, "ARM" would
match inside "Army"), corrupting weekly trend anchor entities -- two
unrelated OpenAI announcements were spuriously linked by a fabricated
"Intel" anchor neither of them was actually about."""

from __future__ import annotations

from news_intel.pipeline.entities import extract_entities, extract_known_entities

_KNOWN = {"Intel", "ARM", "NVIDIA"}


def test_intel_does_not_match_inside_intelligence():
    text = "GPT-5.6 improves AI efficiency, helping deliver more useful intelligence per dollar."
    assert "Intel" not in extract_entities(text, _KNOWN)
    assert "Intel" not in extract_known_entities(text, _KNOWN)


def test_arm_does_not_match_inside_army():
    text = "The U.S. Army Corps of Engineers completed an inventory of historical records."
    assert "ARM" not in extract_entities(text, _KNOWN)
    assert "ARM" not in extract_known_entities(text, _KNOWN)


def test_intel_still_matches_as_a_real_standalone_word():
    text = "Intel announced a new manufacturing partnership for advanced chips."
    assert "Intel" in extract_entities(text, _KNOWN)
    assert "Intel" in extract_known_entities(text, _KNOWN)


def test_arm_still_matches_as_a_real_standalone_word():
    text = "ARM licensed its new chip architecture to several manufacturers."
    assert "ARM" in extract_entities(text, _KNOWN)
    assert "ARM" in extract_known_entities(text, _KNOWN)


def test_single_sentence_initial_titlecase_word_is_not_a_fallback_entity():
    """Retry 1 Blocker 2 re-verification: single Title-Case words at a
    sentence/title start ("Fine-Tuning...", "Building an...", "Announcing
    our...", "Our approach to...") were being treated as candidate
    fallback entities, which -- once company names were correctly removed
    as valid sole trend anchors -- let sentence-starter words like "Fine",
    "Building", "Announcing", "Investing", and "Our" spuriously anchor
    completely unrelated weekly trends together."""
    assert extract_entities("Building an autonomous financial analyst with agents", set()) == []
    assert extract_entities("Announcing the safety fellowship program", set()) == []
    assert extract_entities("Our approach to bioresilience", set()) == []


def test_multi_word_capitalized_sequence_still_extracted_as_fallback_entity():
    assert "Ilya Sutskever" in extract_entities("Ilya Sutskever to leave OpenAI", {"OpenAI"})


def test_single_all_caps_acronym_still_extracted_as_fallback_entity():
    assert "TSMC" in extract_entities("TSMC reported record quarterly revenue growth", set())


def test_single_word_with_digit_still_extracted_as_fallback_entity():
    assert "GB200" in extract_entities("GB200 delivers major performance gains", set())
