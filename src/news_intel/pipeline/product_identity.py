"""Gate A remediation Retry 1 Blocker 2: high-significance product/version
identifiers (e.g. "GPT-5.5" vs "GPT-5.4") that must never be treated as
the same real-world development just because their titles are otherwise
near-identical templated announcements.

Retry 1 found "Introducing GPT-5.5" / "GPT-5.4" / "GPT-5.2" / "GPT-5" /
"GPTs" merged into one same-event cluster (fuzzy title similarity alone
was enough -- "GPT-5" vs "GPTs" alone scores 90.9/100 on the existing
title-similarity metric), and a weekly trend that grouped GPT-5.5, a
differently-versioned Codex model, and unrelated ChatGPT feature launches
under one shared anchor. The root cause is the same in both cases: a
shared product *family* name (or a high title-similarity score) is not
enough to prove two items describe the same product/version -- a
conflicting explicit version number must always win, and a bare/plural
mention of a family ("GPTs", the custom-GPTs feature) is a *different*
concept from any specific numbered release of that family, not a fuzzy
match for one.

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


@dataclass(frozen=True)
class ProductIdentity:
    versions: dict[str, frozenset[str]] = field(default_factory=dict)
    bare_plural_families: frozenset[str] = frozenset()


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
    return ProductIdentity(
        versions={k: frozenset(v) for k, v in versions.items()},
        bare_plural_families=frozenset(bare),
    )


def has_conflicting_product_version(a, b) -> bool:
    """True if the two sides describe incompatible product/version claims:
    an explicit, disjoint version for the same family (a={"GPT": {"5.5"}}
    vs b={"GPT": {"5.4"}}), or one side names a specific version of a
    family while the other only mentions that family as a bare plural
    ("GPTs" is not a fuzzy match for any specific "GPT-N"). Accepts either
    plain version dicts (dict[str, set[str]], e.g. from
    extract_product_versions) or ProductIdentity instances -- callers that
    don't need the bare-plural check can pass plain dicts."""
    a_versions = a.versions if isinstance(a, ProductIdentity) else a
    b_versions = b.versions if isinstance(b, ProductIdentity) else b
    a_bare = a.bare_plural_families if isinstance(a, ProductIdentity) else frozenset()
    b_bare = b.bare_plural_families if isinstance(b, ProductIdentity) else frozenset()

    for family, versions_a in a_versions.items():
        versions_b = b_versions.get(family)
        if versions_b and versions_a.isdisjoint(versions_b):
            return True
        if family in b_bare:
            return True
    for family in b_versions:
        if family in a_bare:
            return True
    return False
