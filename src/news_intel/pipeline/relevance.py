"""Maps matched topic-group vocabulary (config/topics.yaml) onto the broad
relevance buckets and multipliers from config/policy.yaml (section 7.2 of the
design doc). Priority order follows the product priority: AI/semiconductors
and export controls first, then platforms/markets, then policy/geopolitics,
then everything else."""

from __future__ import annotations

from dataclasses import dataclass

from news_intel.settings import TopicsConfig

_BUCKET_PRIORITY = [
    ("ai_semiconductor_export_controls", ["ai", "semiconductors", "policy"]),
    ("tech_platforms_datacenter", []),
    ("markets_macro_corporate", ["macro_markets"]),
    ("us_china_policy_geopolitics", ["china", "geopolitics"]),
]

_ROUTINE_POLITICS_TERMS = ["congress", "senate", "campaign", "rally", "poll numbers"]


@dataclass
class RelevanceMatch:
    bucket: str
    multiplier: float
    matched_terms: list[str]
    matched_entities: list[str]


def _all_terms(group: dict) -> list[str]:
    return list(group.get("terms_en", []) or []) + list(group.get("terms_zh", []) or [])


def classify_relevance(
    text: str, topics: TopicsConfig, multipliers: dict[str, float]
) -> RelevanceMatch:
    lowered = text.lower()

    for term in topics.noise_exclusions:
        if term.lower() in lowered:
            return RelevanceMatch("explicit_noise", multipliers.get("explicit_noise", 0.0), [term], [])

    for bucket, group_names in _BUCKET_PRIORITY:
        matched: list[str] = []
        for group_name in group_names:
            group = topics.groups.get(group_name, {})
            for term in _all_terms(group):
                if term.lower() in lowered or term in text:  # zh terms: substring match, case-sensitive
                    matched.append(term)
        if matched:
            return RelevanceMatch(bucket, multipliers.get(bucket, 1.0), matched, [])

    for term in _ROUTINE_POLITICS_TERMS:
        if term in lowered:
            return RelevanceMatch(
                "routine_politics_general", multipliers.get("routine_politics_general", 0.7), [term], []
            )

    return RelevanceMatch("other_consequential_world", multipliers.get("other_consequential_world", 1.0), [], [])
