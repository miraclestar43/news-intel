"""Gate A remediation Retry 1 Blocker 2: high-significance product/version
identifiers (e.g. "GPT-5.5" vs "GPT-5.4", "ChatGPT Health" vs "ChatGPT
Pulse") that must never be treated as the same real-world development
just because their titles are otherwise near-identical templated
announcements.

Retry 1 found "Introducing GPT-5.5" / "GPT-5.4" / "GPT-5.2" / "GPT-5" /
"GPTs" merged into one same-event cluster (fuzzy title similarity alone
was enough -- "GPT-5" vs "GPTs" alone scores 90.9/100 on the existing
title-similarity metric), and a weekly trend that grouped GPT-5.5, a
differently-versioned Codex model, and unrelated ChatGPT feature launches
under one shared anchor. Re-verifying against real production data
(Retry 1's own collected articles) surfaced the same root cause one level
deeper: OpenAI's "Introducing ChatGPT <feature>" title template alone
pushes trigram similarity well above any reasonable topical-compatibility
floor (e.g. "ChatGPT Health" vs "ChatGPT Pulse" scores 0.59 trigram
overlap purely from the shared template), so two completely unrelated
feature launches (Health vs Pulse, Live vs search) still merged into one
12-member "trend" even after the numeric-version fix.

The fix in both cases is the same principle: a shared product *family*
name (or a high title-similarity/trigram score) is not enough to prove
two items describe the same product/feature -- an explicit, conflicting
identifier must always win. Three identifier shapes are covered:

  - numeric version: "GPT-5.5" vs "GPT-5.4"
  - bare plural of a family: "GPTs" is a different concept from any
    specific numbered release, not a fuzzy match for one
  - named feature suffix: "ChatGPT Health" vs "ChatGPT Pulse" vs
    "GPT-Live" vs "ChatGPT search" -- the word (or short phrase)
    immediately following the GPT/ChatGPT brand stem

Used by both pipeline/cluster.py (same-event clustering) and
pipeline/trends.py (multi-event trend grouping).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# PRODUCT-VERSION, e.g. "GPT-5.5", "GPT-5", "Llama-3.1", "Claude-4". Requires
# an explicit hyphen + digit(s) -- "GPTs" (plural, no version) deliberately
# never matches this pattern, so it can never be treated as any numbered
# GPT model.
_VERSION_RE = re.compile(r"\b([A-Z][A-Za-z]*)-(\d+(?:\.\d+)*)\b")

# A bare plural of an acronym-like family, e.g. "GPTs" -> stem "GPT". This
# captures the generic-feature sense ("GPTs" = the custom-GPTs product)
# that must not be conflated with a specific numbered release.
_BARE_PLURAL_RE = re.compile(r"\b([A-Z]{2,})s\b")

# A named feature immediately following the GPT/ChatGPT brand stem, e.g.
# "ChatGPT Health" -> "HEALTH", "GPT-Live" -> "LIVE", "ChatGPT search" ->
# "SEARCH" (case-insensitive: real titles are inconsistently capitalized).
# Does not match a numeric version ("GPT-5.5") since \d is excluded from
# the suffix class, so this never overlaps with _VERSION_RE.
_BRAND_FEATURE_RE = re.compile(r"\b(?:Chat)?GPT[\s-]+([A-Za-z][A-Za-z-]*)\b", re.IGNORECASE)

# Feature words too generic to distinguish one launch from another (e.g.
# "Introducing new ChatGPT features" mentioning the umbrella word itself).
_GENERIC_FEATURE_WORDS = {"FOR", "AND", "THE", "WITH", "NEW", "FEATURE", "FEATURES", "UPDATE", "UPDATES"}


@dataclass(frozen=True)
class ProductIdentity:
    versions: dict[str, frozenset[str]] = field(default_factory=dict)
    bare_plural_families: frozenset[str] = frozenset()
    brand_feature_suffixes: frozenset[str] = frozenset()


def extract_product_versions(text: str) -> dict[str, set[str]]:
    """Maps product family (upper-cased, e.g. "GPT") to the set of specific
    version strings (e.g. {"5.5"}) explicitly mentioned in the text. A
    family absent from the result means the text made no specific-version
    claim about that family (compatible with anything -- not a conflict).
    Kept as a plain dict[str, set[str]] (rather than ProductIdentity) since
    this is also useful standalone, e.g. for tests."""
    families: dict[str, set[str]] = {}
    for match in _VERSION_RE.finditer(text):
        family, version = match.group(1).upper(), match.group(2)
        families.setdefault(family, set()).add(version)
    return families


def extract_product_identity(text: str) -> ProductIdentity:
    versions = extract_product_versions(text)
    bare = {m.group(1).upper() for m in _BARE_PLURAL_RE.finditer(text)}
    bare -= set(versions)  # a family with an explicit version isn't "bare"

    suffixes = {
        m.group(1).upper() for m in _BRAND_FEATURE_RE.finditer(text)
    } - _GENERIC_FEATURE_WORDS

    return ProductIdentity(
        versions={k: frozenset(v) for k, v in versions.items()},
        bare_plural_families=frozenset(bare),
        brand_feature_suffixes=frozenset(suffixes),
    )


def has_conflicting_product_version(a, b) -> bool:
    """True if the two sides describe incompatible product/version/feature
    claims: an explicit, disjoint version for the same family (a={"GPT":
    {"5.5"}} vs b={"GPT": {"5.4"}}); one side names a specific version of a
    family while the other only mentions that family as a bare plural
    ("GPTs" is not a fuzzy match for any specific "GPT-N"); or -- when both
    sides are ProductIdentity instances -- disjoint named feature suffixes
    of the same brand stem ("ChatGPT Health" vs "ChatGPT Pulse"). Accepts
    either plain version dicts (dict[str, set[str]], e.g. from
    extract_product_versions) or ProductIdentity instances -- callers that
    don't need the bare-plural/feature-suffix checks can pass plain
    dicts."""
    a_versions = a.versions if isinstance(a, ProductIdentity) else a
    b_versions = b.versions if isinstance(b, ProductIdentity) else b
    a_bare = a.bare_plural_families if isinstance(a, ProductIdentity) else frozenset()
    b_bare = b.bare_plural_families if isinstance(b, ProductIdentity) else frozenset()
    a_suffixes = a.brand_feature_suffixes if isinstance(a, ProductIdentity) else frozenset()
    b_suffixes = b.brand_feature_suffixes if isinstance(b, ProductIdentity) else frozenset()

    for family, versions_a in a_versions.items():
        versions_b = b_versions.get(family)
        if versions_b and versions_a.isdisjoint(versions_b):
            return True
        if family in b_bare:
            return True
    for family in b_versions:
        if family in a_bare:
            return True

    if a_suffixes and b_suffixes and a_suffixes.isdisjoint(b_suffixes):
        return True

    # A numbered-version claim for the GPT family on one side and a named
    # feature-suffix claim on the other are also different, specific
    # developments -- "GPT-5.3-Codex-Spark" is not "ChatGPT Pro". (The
    # brand-feature regex only ever matches the GPT/ChatGPT stem, so any
    # non-empty suffix set is implicitly "for GPT".)
    if a_versions.get("GPT") and b_suffixes:
        return True
    if b_versions.get("GPT") and a_suffixes:
        return True

    return False
