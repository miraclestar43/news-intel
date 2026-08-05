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


# --- named feature suffix (Retry 1 Blocker 2 re-verification finding) ---


def test_named_feature_suffix_extracted_from_chatgpt_title():
    identity = extract_product_identity("Introducing ChatGPT Health")
    assert identity.brand_feature_suffixes == {"HEALTH"}


def test_named_feature_suffix_extracted_case_insensitively():
    identity = extract_product_identity("Introducing ChatGPT search")
    assert identity.brand_feature_suffixes == {"SEARCH"}


def test_named_feature_suffix_extracted_from_hyphenated_gpt_title():
    identity = extract_product_identity("Introducing GPT-Live")
    assert identity.brand_feature_suffixes == {"LIVE"}


def test_disjoint_feature_suffixes_of_the_same_brand_conflict():
    """"ChatGPT Health" and "ChatGPT Pulse" alone score 0.59 trigram
    overlap purely from the shared "Introducing ChatGPT" template -- an
    explicit, different named feature must still win."""
    health = extract_product_identity("Introducing ChatGPT Health")
    pulse = extract_product_identity("Introducing ChatGPT Pulse")
    assert has_conflicting_product_version(health, pulse) is True


def test_a_version_claim_conflicts_with_a_named_feature_suffix():
    """"GPT-5.3-Codex-Spark" (a specific numbered release) and "ChatGPT
    Pro" (a named feature, no version) make different, specific claims --
    neither the version-conflict rule nor the bare-plural rule alone
    catches this mixed case, so it needs its own check."""
    versioned = extract_product_identity("Introducing GPT-5.3-Codex-Spark")
    named = extract_product_identity("Introducing ChatGPT Pro")
    assert versioned.brand_feature_suffixes == frozenset()  # no suffix match on a numbered release
    assert has_conflicting_product_version(versioned, named) is True
    assert has_conflicting_product_version(named, versioned) is True


def test_same_named_feature_suffix_is_not_a_conflict():
    a = extract_product_identity("Introducing ChatGPT Health")
    b = extract_product_identity("OpenAI rolls out ChatGPT Health to all users")
    assert has_conflicting_product_version(a, b) is False


def test_no_suffix_claim_on_either_side_is_not_a_conflict():
    a = extract_product_identity("How ChatGPT adoption has expanded")
    b = extract_product_identity("What people build with ChatGPT")
    assert has_conflicting_product_version(a, b) is False
