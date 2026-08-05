"""Gate A remediation Retry 1 Blocker 2: product/version identifier
extraction and conflict detection, the pure logic underneath the same-
event and weekly-trend fixes."""

from __future__ import annotations

from news_intel.pipeline.product_identity import (
    extract_product_identity,
    extract_product_versions,
    has_conflicting_product_version,
)


def test_extracts_hyphenated_version_numbers():
    versions = extract_product_versions("Introducing GPT-5.5")
    assert versions == {"GPT": {"5.5"}}


def test_gpts_plural_does_not_normalize_to_any_numbered_model():
    versions = extract_product_versions("Introducing GPTs")
    assert versions == {}


def test_bare_product_mention_without_version_extracts_nothing():
    versions = extract_product_versions("How ChatGPT adoption has expanded")
    assert versions == {}


def test_multiple_families_in_one_text():
    versions = extract_product_versions("GPT-5.5 vs Llama-3.1 benchmark comparison")
    assert versions == {"GPT": {"5.5"}, "LLAMA": {"3.1"}}


def test_conflicting_versions_of_same_family_detected():
    a = extract_product_versions("Introducing GPT-5.5")
    b = extract_product_versions("Introducing GPT-5.4")
    assert has_conflicting_product_version(a, b) is True


def test_same_version_is_not_a_conflict():
    a = extract_product_versions("Introducing GPT-5.5")
    b = extract_product_versions("OpenAI ships GPT-5.5 to enterprise customers")
    assert has_conflicting_product_version(a, b) is False


def test_one_side_with_no_version_claim_is_not_a_conflict():
    a = extract_product_versions("Introducing GPT-5.5")
    b = extract_product_versions("Introducing GPTs")  # no version claim at all
    assert has_conflicting_product_version(a, b) is False


def test_different_families_never_conflict():
    a = extract_product_versions("Introducing GPT-5.5")
    b = extract_product_versions("Introducing Llama-3.1")
    assert has_conflicting_product_version(a, b) is False


def test_bare_plural_family_extracted_separately_from_versions():
    identity = extract_product_identity("Introducing GPTs")
    assert identity.versions == {}
    assert identity.bare_plural_families == {"GPT"}


def test_a_family_with_an_explicit_version_is_not_also_counted_as_bare():
    identity = extract_product_identity("GPT-5.5 outperforms GPTs on every benchmark")
    assert identity.versions == {"GPT": frozenset({"5.5"})}
    assert "GPT" not in identity.bare_plural_families


def test_bare_plural_conflicts_with_a_specific_version_of_the_same_family():
    """"GPTs" (the custom-GPTs feature) is a different concept from any
    specific numbered GPT release, not a fuzzy match for one -- even
    though "GPT-5" vs "GPTs" alone scores 90.9/100 on title similarity."""
    versioned = extract_product_identity("Introducing GPT-5")
    bare = extract_product_identity("Introducing GPTs")
    assert has_conflicting_product_version(versioned, bare) is True
    assert has_conflicting_product_version(bare, versioned) is True


def test_bare_plural_of_an_unrelated_family_does_not_conflict():
    versioned = extract_product_identity("Introducing GPT-5")
    bare = extract_product_identity("Introducing new APIs")  # "API" family, unrelated to GPT
    assert has_conflicting_product_version(versioned, bare) is False
