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
from dataclasses import dataclass, field
from functools import lru_cache

from news_intel.pipeline.materiality import HIGH_IMPACT_TERMS
from news_intel.settings import LegalNoticeBoilerplatePolicy, TopicsConfig

_BUCKET_PRIORITY = [
    ("ai_semiconductor_export_controls", ["ai", "semiconductors", "policy"]),
    ("tech_platforms_datacenter", []),
    ("markets_macro_corporate", ["macro_markets"]),
    # 2026-08-17: "geopolitics" moved OUT of this bucket's group list (was
    # ["china", "geopolitics"]) -- a Middle East/Ukraine/Korea story with no
    # China-specific vocabulary must not be labeled "US / China / World"
    # just because it also matched a general term like "ceasefire" or
    # "missile". China-specific classification now depends on the "china"
    # group alone; general world conflict/diplomacy vocabulary (the old
    # "geopolitics" group, unchanged) now feeds the separately-gated
    # world_politics_geopolitics bucket below instead -- see
    # _WORLD_POLITICS_GROUPS.
    ("us_china_policy_geopolitics", ["china"]),
]

# 2026-08-17 Politics/World classification repair (BBC audit: Lebanon
# strikes and Korean-peninsula diplomacy candidates remained Unclassified).
# Deliberately NOT folded into _BUCKET_PRIORITY: unlike every bucket above,
# a bare vocabulary hit here is never sufficient on its own -- see
# _world_politics_bucket, which additionally requires a materially
# consequential event-type signal (materiality.HIGH_IMPACT_TERMS) before
# this bucket can fire, so sports/entertainment/lifestyle/minor-accident/
# routine-statement/soft-news coverage set in one of these countries still
# fails the gate. This is a general event-type/actor rule -- never a
# title-specific Lebanon or Korea exception. Checked AFTER the tariff-
# context check (classify_relevance) so a generic trade-war/tariff story
# still resolves via the more specific tariff-context logic first.
WORLD_POLITICS_BUCKET = "world_politics_geopolitics"
_WORLD_POLITICS_GROUPS = ["world_politics", "geopolitics"]

_ROUTINE_POLITICS_TERMS = ["congress", "senate", "campaign", "rally", "poll numbers"]

UNCLASSIFIED_BUCKET = "unclassified"


@dataclass
class RelevanceMatch:
    bucket: str
    multiplier: float
    matched_terms: list[str]
    matched_entities: list[str]
    # 2026-08-17 Politics/World classification repair: every bucket whose
    # vocabulary matched (not just the winning `bucket`), separate from the
    # single-valued primary display category -- multi-label coverage tags,
    # e.g. a story can be tagged both "china" and "world_politics" even
    # though only one wins the primary category slot.
    coverage_tags: list[str] = field(default_factory=list)


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


def _match_buckets(text: str, topics: TopicsConfig) -> tuple[str, list[str]] | None:
    """The first bucket (priority order) with any vocabulary hit in `text`,
    or None. Factored out of classify_relevance so it can be tried against
    the title alone before falling back to the full combined text."""
    lowered = text.lower()
    for bucket, group_names in _BUCKET_PRIORITY:
        matched: list[str] = []
        for group_name in group_names:
            group = topics.groups.get(group_name, {})
            for term in _all_terms(group):
                if _term_matches(term, lowered, text):
                    matched.append(term)
        if matched:
            return bucket, matched
    return None


def _has_event_type_signal(lowered: str) -> bool:
    return any(term in lowered for term in HIGH_IMPACT_TERMS)


def _world_politics_bucket(text: str, lowered: str, topics: TopicsConfig) -> tuple[str, list[str]] | None:
    """General world-politics/geopolitics classification: requires BOTH (a)
    a country/region/capital/government/international-organization/
    geopolitical-actor vocabulary hit (config/topics.yaml's world_politics
    group) AND (b) a materially consequential event-type signal (conflict,
    strike, ceasefire, negotiation, treaty, sanctions, election, court
    ruling, regulation, military action, diplomatic recognition, leadership
    change, ... -- materiality.HIGH_IMPACT_TERMS). A bare country/actor
    mention alone (e.g. a sports/entertainment/lifestyle story set in one
    of these places) never qualifies on its own."""
    matched: list[str] = []
    for group_name in _WORLD_POLITICS_GROUPS:
        group = topics.groups.get(group_name, {})
        for term in _all_terms(group):
            if _term_matches(term, lowered, text):
                matched.append(term)
    if not matched:
        return None
    if not _has_event_type_signal(lowered):
        return None
    return WORLD_POLITICS_BUCKET, matched


def _coverage_tags(text: str, lowered: str, topics: TopicsConfig, primary_bucket: str) -> list[str]:
    """Every bucket (from _BUCKET_PRIORITY plus world_politics) whose
    vocabulary matched anywhere in `text`, not just the single winning
    bucket used as the primary display category -- multi-label coverage,
    kept separate from that primary category. Always includes
    `primary_bucket` first when it isn't already one of these (e.g. noise/
    tariff/routine-politics/unclassified outcomes computed via a different
    path)."""
    tags: list[str] = []
    for bucket, group_names in _BUCKET_PRIORITY:
        for group_name in group_names:
            group = topics.groups.get(group_name, {})
            if any(_term_matches(term, lowered, text) for term in _all_terms(group)):
                if bucket not in tags:
                    tags.append(bucket)
                break
    for group_name in _WORLD_POLITICS_GROUPS:
        group = topics.groups.get(group_name, {})
        if any(_term_matches(term, lowered, text) for term in _all_terms(group)):
            if WORLD_POLITICS_BUCKET not in tags:
                tags.append(WORLD_POLITICS_BUCKET)
            break
    if primary_bucket not in tags:
        tags.insert(0, primary_bucket)
    return tags


def classify_relevance(
    text: str, topics: TopicsConfig, multipliers: dict[str, float], *, title: str | None = None
) -> RelevanceMatch:
    lowered = text.lower()

    for term in topics.noise_exclusions:
        # 2026-08-13 fix: this loop used a bare substring check (`term in
        # lowered`) despite the module's own word-boundary discipline
        # everywhere else -- "sale" matched inside "sales", silently
        # zeroing out (noise_penalty=100, final_score=0) a Nebius earnings
        # report whose only offending word was "second-quarter sales".
        if _term_matches(term, lowered, lowered):
            return RelevanceMatch(
                "explicit_noise", multipliers.get("explicit_noise", 0.0), [term], [], ["explicit_noise"]
            )

    if is_legal_notice_boilerplate(text, topics.legal_notice_boilerplate):
        return RelevanceMatch(
            "explicit_noise", multipliers.get("explicit_noise", 0.0), ["legal_notice_boilerplate"], [],
            ["explicit_noise"],
        )

    # 2026-08-15 defect: an event whose title is squarely a Markets &
    # Economy earnings-quality analysis (Amazon/Alphabet "other income")
    # was classified as AI & Semiconductors purely because "Anthropic"
    # appeared once, in a background clause of the excerpt, as one example
    # of the investments driving those paper gains -- not because the
    # story is about Anthropic or AI at all. The title is what a story is
    # actually about; matching it first (when it alone yields a bucket)
    # takes priority over a vocabulary term that only appears in
    # supporting/background body text. Falls back to the combined text
    # (unchanged prior behavior) whenever the title alone is inconclusive.
    if title:
        title_match = _match_buckets(title, topics)
        if title_match is not None:
            bucket, matched = title_match
            tags = _coverage_tags(text, lowered, topics, bucket)
            return RelevanceMatch(bucket, multipliers.get(bucket, 1.0), matched, [], tags)

    full_match = _match_buckets(text, topics)
    if full_match is not None:
        bucket, matched = full_match
        tags = _coverage_tags(text, lowered, topics, bucket)
        return RelevanceMatch(bucket, multipliers.get(bucket, 1.0), matched, [], tags)

    # Checked before the general world-politics gate: a generic trade-war/
    # tariff story resolves via this more specific tariff-context logic
    # first (unchanged prior behavior).
    tariff_bucket = _trade_tariff_bucket(lowered, topics)
    if tariff_bucket is not None:
        tags = _coverage_tags(text, lowered, topics, tariff_bucket)
        return RelevanceMatch(tariff_bucket, multipliers.get(tariff_bucket, 1.0), ["tariff"], [], tags)

    # Feed/category metadata (title-vocabulary match above) is a prior, not
    # automatic qualification -- the world-politics gate below still
    # requires its own independent event-type/consequence signal even when
    # nothing else matched.
    world_match = _world_politics_bucket(text, lowered, topics)
    if world_match is not None:
        bucket, matched = world_match
        tags = _coverage_tags(text, lowered, topics, bucket)
        return RelevanceMatch(bucket, multipliers.get(bucket, 1.0), matched, [], tags)

    for term in _ROUTINE_POLITICS_TERMS:
        if term in lowered:
            tags = _coverage_tags(text, lowered, topics, "routine_politics_general")
            return RelevanceMatch(
                "routine_politics_general", multipliers.get("routine_politics_general", 0.7), [term], [], tags
            )

    tags = _coverage_tags(text, lowered, topics, UNCLASSIFIED_BUCKET)
    return RelevanceMatch(UNCLASSIFIED_BUCKET, multipliers.get(UNCLASSIFIED_BUCKET, 0.0), [], [], tags)
