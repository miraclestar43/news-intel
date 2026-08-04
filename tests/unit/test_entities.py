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
