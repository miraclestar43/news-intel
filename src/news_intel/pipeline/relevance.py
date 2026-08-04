"""Maps matched topic-group vocabulary (config/topics.yaml) onto the broad
relevance buckets and multipliers from config/policy.yaml (section 7.2 of the
design doc). Priority order follows the product priority: AI/semiconductors
and export controls first, then platforms/markets, then policy/geopolitics,
then everything else.

Gate A remediation Phase 3: matching uses token/phrase boundaries (never a
bare substring, so e.g. "gpt" cannot match inside an unrelated word), bare
"tariff" requires contextual trade-policy evidence rather than standing
alone, and anything that matches nothing is honestly reported as
"unclassified" rather than silently defaulting to "World" with full
relevance weight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from news_intel.settings import LegalNoticeBoilerplatePolicy, TopicsConfig

_BUCKET_PRIORITY = [
    ("ai_semiconductor_export_controls", ["ai", "semiconductors", "policy"]),
    ("tech_platforms_datacenter", []),
    ("markets_macro_corporate", ["macro_markets"]),
    ("us_china_policy_geopolitics", ["china", "geopolitics"]),
]

_ROUTINE_POLITICS_TERMS = ["congress", "senate", "campaign", "rally", "poll numbers"]

UNCLASSIFIED_BUCKET = "unclassified"


@dataclass
class RelevanceMatch:
    bucket: str
    multiplier: float
    matched_terms: list[str]
    matched_entities: list[str]


def _all_terms(group: dict) -> list[str]:
    return list(group.get("terms_en", []) or []) + list(group.get("terms_zh", []) or [])


@lru_cache(maxsize=4096)
def _term_pattern(term: str) -> re.Pattern:
    # \b works for ASCII-vocabulary terms (including multi-word phrases,
    # where it anchors at the phrase's own first/last characters). CJK terms
    # have no word-boundary concept in Python's \b, so they fall back to
    # plain substring matching exactly as before.
    return re.compile(rf"\b{re.escape(term.lower())}\b")


def _term_matches(term: str, lowered: str, original: str) -> bool:
    if term.isascii():
        return bool(_term_pattern(term).search(lowered))
    return term in original  # zh terms: substring match, case-sensitive


def _any_phrase_present(phrases: list[str], lowered: str) -> bool:
    return any(phrase.lower() in lowered for phrase in phrases)


def is_legal_notice_boilerplate(text: str, policy: LegalNoticeBoilerplatePolicy) -> bool:
    """Public so pipeline/cluster.py can apply the same detection when
    deciding whether two items are allowed to cluster as one event (Gate A
    remediation Phase 3.4) -- not just for scoring/category classification."""
    lowered = text.lower()
    if not _any_phrase_present(policy.phrases, lowered):
        return False
    return not _any_phrase_present(policy.material_override_phrases, lowered)


def _trade_tariff_bucket(lowered: str, topics: TopicsConfig) -> str | None:
    """Returns the bucket a genuine (contextually-supported) tariff/trade
    story belongs in, or None if bare "tariff" has no supporting context (or
    matches an administrative/utility filing) and therefore contributes
    nothing on its own."""
    ctx = topics.trade_policy_context
    anchors = ctx.anchor_terms_en + ctx.anchor_terms_zh
    if not any(_term_matches(term, lowered, lowered) for term in anchors):
        return None
    if _any_phrase_present(ctx.negative_context_en, lowered):
        return None
    if any(_term_matches(term, lowered, lowered) for term in ctx.ai_semiconductor_context_terms_en):
        return "ai_semiconductor_export_controls"
    if any(_term_matches(term, lowered, lowered) for term in ctx.context_terms_en):
        return "us_china_policy_geopolitics"
    return None  # bare "tariff" with no supporting context at all


def classify_relevance(
    text: str, topics: TopicsConfig, multipliers: dict[str, float]
) -> RelevanceMatch:
    lowered = text.lower()

    for term in topics.noise_exclusions:
        if term.lower() in lowered:
            return RelevanceMatch("explicit_noise", multipliers.get("explicit_noise", 0.0), [term], [])

    if is_legal_notice_boilerplate(text, topics.legal_notice_boilerplate):
        return RelevanceMatch(
            "explicit_noise", multipliers.get("explicit_noise", 0.0), ["legal_notice_boilerplate"], []
        )

    for bucket, group_names in _BUCKET_PRIORITY:
        matched: list[str] = []
        for group_name in group_names:
            group = topics.groups.get(group_name, {})
            for term in _all_terms(group):
                if _term_matches(term, lowered, text):
                    matched.append(term)
        if matched:
            return RelevanceMatch(bucket, multipliers.get(bucket, 1.0), matched, [])

    tariff_bucket = _trade_tariff_bucket(lowered, topics)
    if tariff_bucket is not None:
        return RelevanceMatch(tariff_bucket, multipliers.get(tariff_bucket, 1.0), ["tariff"], [])

    for term in _ROUTINE_POLITICS_TERMS:
        if term in lowered:
            return RelevanceMatch(
                "routine_politics_general", multipliers.get("routine_politics_general", 0.7), [term], []
            )

    return RelevanceMatch(UNCLASSIFIED_BUCKET, multipliers.get(UNCLASSIFIED_BUCKET, 0.0), [], [])
