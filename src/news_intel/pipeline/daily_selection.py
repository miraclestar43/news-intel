"""Daily-digest story selection. Applied to the SAME already-qualified
candidate pool (`event_repo.qualified_events_since`, unchanged threshold)
that jobs/daily.py has always used -- this module never lowers or bypasses
qualification, it only decides which qualified events become one digest
slot, in what order, and whether a repeat of an already-delivered story is
suppressed or shown with what changed.

Selection order:
  0. Cross-day novelty -- _apply_cross_day_novelty() drops a candidate that
     is the same underlying event as a story delivered in the last several
     daily digests, UNLESS pipeline/story_identity.detect_material_update()
     finds a concrete, textually-grounded delta since that prior telling
     (2026-08-12 repair: the Aug 10 NVIDIA financing announcement and its
     Aug 12 recap under a new headline/URL were, before this, both
     independently selectable as "new" stories).
  1. Qualification and source validation -- already done by the caller
     (qualified_events_since + jobs/daily.py's own category/URL checks).
  2. Story-level clustering/deduplication -- group_story_families(), using
     pipeline/story_identity.same_event_story() (2026-08-12 repair: replaces
     the 2026-08-11 same-domain + same-day + shared-capitalized-token
     heuristic, which had no filter for a shared *company* name and so
     falsely merged two unrelated NVIDIA blog posts; see that module's
     docstring for the full root-cause account).
  3. Cross-category coverage -- one family per required category, from the
     highest-decision-value qualified family in that category, if one
     exists.
  4. Decision-value-based filling of remaining positions -- respecting the
     AI-family cap and the per-publisher-family cap (pipeline/
     publisher_family.py: registrable-domain based, 2026-08-12 repair --
     replaces the 2026-08-11 exact-hostname cap, which let blogs.nvidia.com
     and developer.nvidia.com count as two different sources). Ranking uses
     _decision_value() (materiality/relevance/market-impact), NOT
     final_score (2026-08-14 repair: final_score blends in confirmation_
     quality/corroboration at a combined weight -- 0.15 + 0.10 -- that let
     a routine T0 vendor blog post consistently outrank a much more
     material but single-sourced story purely on source tier; final_score
     remains the qualification threshold, unchanged, but is no longer the
     ranking key once a candidate pool is already qualified).
  5. Render-readiness gate (_apply_render_readiness_gate, 2026-08-16
     repair) -- decision-value ordering from steps 3-4 is computed and
     fixed BEFORE this runs, so summary availability never influences
     *which* families were selected; but a selected story for which
     enrichment (primary source, then linked secondary source --
     pipeline/summary_quality.best_available_summary) still produces no
     complete "what happened" text is dropped from delivery and
     backfilled with the next qualified, render-ready family (re-checked
     against the AI-family/publisher-family caps). Runs before the TOP/
     MORE split specifically so TOP can never end up empty while MORE is
     non-empty -- see that function's docstring for the exact 2026-08-15
     regression this replaced.
  6. Final rendering validation -- the caller (jobs/daily.py) already
     re-validates category/URL/length before ever contacting Telegram;
     this module's caps are enforced constructively, not just checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from news_intel.pipeline.editorial_enrichment import EditorialResult
from news_intel.pipeline.publisher_family import publisher_family_of_url
from news_intel.pipeline.render_readiness import is_render_ready
from news_intel.pipeline.story_identity import detect_material_update, same_event_story
from news_intel.pipeline.zh_render_readiness import is_render_ready_zh
from news_intel.settings import AlertTriggersConfig

MAX_TOP_STORIES = 3
MAX_TOTAL_STORIES = 8
MIN_TOTAL_STORIES = 4
MAX_AI_STORIES = 2
MAX_STORIES_PER_DOMAIN = 2

# 2026-08-22 repair: the Aug 19-21 zh_cn digests consistently shipped only
# 2-3 total stories (well under MAX_TOTAL_STORIES=8) with no indication to
# the reader that the qualified candidate pool was genuinely this thin --
# indistinguishable from a normal, deliberately concise day. The system
# must never force low-quality/promotional filler just to hit a target
# count (see event_integrity_zh.py, the WRC/award-list gates), but silently
# shipping a thin digest is its own failure of disclosure -- the formatter
# appends an explicit UNDERFILLED_QUALIFIED_POOL footer instead whenever
# the total falls below this threshold, so a thin day is never
# indistinguishable from a normal one.
UNDERFILLED_POOL_THRESHOLD = 6
MAX_TOP_STORIES_PER_DOMAIN = 1
MIN_NON_AI_WHEN_FOUR_OR_MORE = 2

TECHNOLOGY_FAMILY_CATEGORIES = {"AI & Semiconductors"}
REQUIRED_COVERAGE_CATEGORIES = ("Markets & Economy", "Politics", "US / China / World", "World")


# Decision-value weights (2026-08-14): materiality dominates, user-relevance
# and market-impact are secondary. Deliberately excludes confirmation_
# quality/corroboration/novelty/popularity/freshness -- those remain part
# of final_score's qualification threshold (a single-sourced, low-
# confidence story still has to clear the same bar to be a candidate at
# all) but must not additionally control *ranking* among already-qualified
# candidates, which is what let source tier consistently beat substance.
_DECISION_VALUE_SIGNIFICANCE_WEIGHT = 0.55
_DECISION_VALUE_USER_RELEVANCE_WEIGHT = 0.25
_DECISION_VALUE_MARKET_IMPACT_WEIGHT = 0.20


def _decision_value(item: dict) -> float:
    """Ranking key for stage 3 (materiality/user-relevance ranking) and
    every later ordering decision in this module. Falls back to
    final_score when an item doesn't carry the score decomposition fields
    (event_repo.event_as_digest_dict always does for real candidates; test
    fixtures that only set final_score still get a sensible ranking)."""
    if "significance" not in item or item.get("significance") is None:
        return item.get("final_score", 0.0) or 0.0
    significance = item.get("significance") or 0.0
    user_relevance = item.get("user_relevance") or 0.0
    market_impact = item.get("market_impact") or 0.0
    return (
        _DECISION_VALUE_SIGNIFICANCE_WEIGHT * significance
        + _DECISION_VALUE_USER_RELEVANCE_WEIGHT * user_relevance
        + _DECISION_VALUE_MARKET_IMPACT_WEIGHT * market_impact
    )


@dataclass
class Story:
    """One digest slot: a primary event dict (event_repo.event_as_digest_dict
    shape) plus, if a story family had more than one member, up to one
    additional linked source kept as a secondary citation. `update_note` is
    set when this story is a legitimate repeat of an already-delivered
    story with a proven material delta (cross-day novelty step 0)."""
    primary: dict
    secondary: dict | None = None
    update_note: str | None = None
    # 2026-08-24: optional per-story LLM editorial result (validator-gated;
    # see pipeline.editorial_enrichment). None for every existing caller
    # that doesn't attempt enrichment -- renderers fall back to their own
    # deterministic why_it_matters()/chinese_takeaway() functions in that
    # case, unchanged from before this field existed.
    editorial: EditorialResult | None = None

    @property
    def event_id(self) -> str:
        return self.primary["event_id"]

    @property
    def category(self) -> str:
        return self.primary["category"]

    @property
    def score(self) -> float:
        return self.primary["final_score"]

    @property
    def decision_value(self) -> float:
        return _decision_value(self.primary)

    @property
    def domain(self) -> str:
        return _publisher_family_of(self.primary)

    @property
    def render_ready(self) -> bool:
        # 2026-08-17 multi-edition architecture: dispatches on the event's
        # own edition_id (event_repo.event_as_digest_dict) -- zh_cn text has
        # no [.!?] sentence-ending punctuation, so applying the English
        # extractor to it would wrongly treat every genuine Chinese story
        # as render-unready and silently empty out the zh_cn digest.
        if self.primary.get("edition_id") == "zh_cn":
            return is_render_ready_zh(self.primary, self.secondary)
        return is_render_ready(self.primary, self.secondary)


def _publisher_family_of(item: dict) -> str:
    """2026-08-15 publisher-family repair: keys on event_repo.event_sources's
    verified `origin_family` (the original outlet's own domain when
    resolvable, e.g. via a Techmeme citation's extracted origin_url) rather
    than re-deriving a family from the stored source URL, which for an
    aggregator-discovered item is the aggregator's own page
    (techmeme.com) -- that collapsed a WSJ-originated and a Bloomberg-
    originated story, both discovered via Techmeme, into one publisher
    family. `origin_family` already falls back to the discovery domain
    conservatively when origin can't be verified (event_repo.event_sources
    docstring). Falls back further to parsing the source URL directly only
    for callers/fixtures that don't carry the origin_family field at all."""
    sources = item.get("sources") or []
    if not sources:
        return ""
    if "origin_family" in sources[0]:
        return sources[0]["origin_family"] or ""
    if sources[0].get("url"):
        return publisher_family_of_url(sources[0]["url"])
    return ""


def _apply_cross_day_novelty(
    candidates: list[dict],
    prior_stories: list[dict],
    *,
    generic_terms: set[str],
    known_entities: set[str],
    alert_triggers: AlertTriggersConfig,
) -> tuple[list[dict], dict[str, str]]:
    """Drops any candidate matching an already-delivered prior story unless
    a material update is found; returns (kept_candidates, {event_id: note})
    for the ones kept *because* of a proven update."""
    if not prior_stories:
        return candidates, {}
    kept: list[dict] = []
    notes: dict[str, str] = {}
    for cand in candidates:
        prior_match = next(
            (p for p in prior_stories if same_event_story(cand, p, generic_terms=generic_terms)), None
        )
        if prior_match is None:
            kept.append(cand)
            continue
        update = detect_material_update(
            cand, prior_match, known_entities=known_entities, alert_triggers=alert_triggers
        )
        if update.is_update:
            kept.append(cand)
            notes[cand["event_id"]] = update.note or ""
        # else: silently suppressed -- a repeat, recap, or restatement of an
        # already-delivered story with no proven new fact.
    return kept, notes


def group_story_families(candidates: list[dict], *, generic_terms: set[str]) -> list[Story]:
    """Complete-link grouping (mirrors pipeline/trends.py's transitive-
    bridging safeguard): a candidate only joins an existing family if it
    matches *every* current member, never just the first. The highest-
    scoring (final_score) member of each family becomes primary -- picking
    the most *complete/authoritative* source to cite is a different
    question from picking the most *material* family, so this stays on
    final_score even though cross-family ranking (below) does not. The
    next-highest becomes the sole secondary citation (a family is a digest
    slot, not an unbounded list). Families themselves are ranked by
    decision value (materiality/relevance), not final_score."""
    groups: list[list[dict]] = []
    for item in candidates:
        target = None
        for group in groups:
            if all(same_event_story(item, member, generic_terms=generic_terms) for member in group):
                target = group
                break
        if target is None:
            groups.append([item])
        else:
            target.append(item)

    stories = []
    for group in groups:
        group.sort(key=lambda i: i["final_score"], reverse=True)
        secondary = group[1] if len(group) > 1 else None
        stories.append(Story(primary=group[0], secondary=secondary))
    stories.sort(key=lambda s: s.decision_value, reverse=True)
    return stories


@dataclass
class SelectionResult:
    top_stories: list[Story] = field(default_factory=list)
    more_to_know: list[Story] = field(default_factory=list)
    # 2026-08-29 production incident: the delivered zh_cn digests told the
    # reader "今日仅有 2 条动态达到质量与来源多样性标准" -- i.e. only two
    # stories met the QUALITY bar -- when the real cause was source
    # concentration (every candidate came from IT之家/量子位, and
    # MAX_STORIES_PER_DOMAIN then capped how many could be shown). Blaming
    # quality for what is actually a publisher-cap outcome misleads the
    # reader about their own feed. These carry the honest counts so the
    # renderer can say which of the three it actually was.
    qualified_family_count: int = 0
    distinct_publisher_families: int = 0

    @property
    def all_stories(self) -> list[Story]:
        return self.top_stories + self.more_to_know

    @property
    def underfill_cause(self) -> str:
        """"quality" | "source_concentration" | "publisher_cap".

        source_concentration: plenty of qualified stories existed but they
        came from very few publishers. publisher_cap: enough publishers,
        but the per-domain cap still bound. quality: the candidate pool
        itself was genuinely thin."""
        shown = len(self.all_stories)
        if self.qualified_family_count <= shown:
            return "quality"
        if self.distinct_publisher_families <= 2:
            return "source_concentration"
        return "publisher_cap"


def _is_ai(story: Story) -> bool:
    return story.category in TECHNOLOGY_FAMILY_CATEGORIES


def _domain_counts(selected: list[Story]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in selected:
        if s.domain:
            counts[s.domain] = counts.get(s.domain, 0) + 1
    return counts


def _can_add(story: Story, selected: list[Story]) -> bool:
    """AI-family cap and the *total* per-publisher-family cap -- both apply
    regardless of eventual top/more-to-know placement, since those are
    decided by final score rank after every position is filled (see
    select_daily_stories). The stricter "at most one per publisher family
    among the eventual top stories" rule is enforced separately, after
    ranking, since which stories end up in the top-3 isn't known until
    selection (which can include coverage picks) is complete."""
    ai_count = sum(1 for s in selected if _is_ai(s))
    if _is_ai(story) and ai_count >= MAX_AI_STORIES:
        return False
    if story.domain and _domain_counts(selected).get(story.domain, 0) >= MAX_STORIES_PER_DOMAIN:
        return False
    return True


def _finalize_top_and_more(selected: list[Story], max_total: int) -> tuple[list[Story], list[Story]]:
    """Constructs TOP and MORE TO KNOW from the fully coverage/cap/novelty-
    filtered `selected` list (already sorted by decision value descending).
    One hard constraint applies to TOP, never as a best-effort swap that
    can still fail (2026-08-13 repair -- the prior swap-based approach left
    two Techmeme-family stories in TOP when no valid swap partner existed):
    at most MAX_TOP_STORIES_PER_DOMAIN stories from the same publisher
    family; a second same-family story moves to MORE TO KNOW (still
    subject to the digest-wide MAX_STORIES_PER_DOMAIN total, already
    enforced when `selected` was built) or is dropped if MORE TO KNOW is
    already at max_total.

    2026-08-14/2026-08-16 repair: summary availability never influences
    which families were *selected* or their decision-value order --
    "summary availability must not cause a lower-value story to outrank a
    higher-value event." That ordering question is fully settled by the
    time `selected` reaches this function; render-readiness (whether a
    story has enough source text to actually render, as opposed to which
    story is more material) is enforced separately, by
    _apply_render_readiness_gate, which runs on `selected` *before* this
    split -- so by the time this function sees it, every story here has
    already passed that gate and is safe to place in TOP with real
    content, never an internal placeholder.

    TOP may end up with fewer than MAX_TOP_STORIES entries -- never padded
    with a story that fails the domain constraint."""
    top: list[Story] = []
    more: list[Story] = []
    top_domains_seen: set[str] = set()
    for story in selected:
        eligible_for_top = len(top) < MAX_TOP_STORIES and (
            not story.domain or story.domain not in top_domains_seen
        )
        if eligible_for_top:
            top.append(story)
            if story.domain:
                top_domains_seen.add(story.domain)
        elif len(top) + len(more) < max_total:
            more.append(story)
    return top, more


def _apply_render_readiness_gate(selected: list[Story], families: list[Story]) -> list[Story]:
    """2026-08-16 repair: runs BEFORE the TOP/MORE split (not after), and
    filters+backfills the whole `selected` list rather than swapping
    within an already-fixed TOP/MORE partition. The prior post-split
    design demoted a render-failed TOP story into MORE TO KNOW and tried
    to backfill TOP from MORE's own leftovers or from unselected
    families -- but when *every* selected story failed render-readiness
    (the exact 2026-08-15 regression: all three candidates' excerpts had
    no [.!?]-terminated sentence under the pre-2026-08-16 stricter rule),
    there was nothing render-ready anywhere to promote, leaving TOP empty
    while the render-failed stories still sat, un-demoted-from-delivery,
    in MORE TO KNOW.

    This version can only ever produce an empty result when the render-
    ready pool is genuinely empty everywhere -- in which case
    _finalize_top_and_more's TOP and MORE are *both* empty, never one
    without the other. A render-failed candidate is dropped from delivery
    entirely here (not silently demoted) and, exactly like the original
    stage-4 fill, backfilled from the next qualified family that is
    render-ready and still respects the AI-family/publisher-family caps
    (re-checked fresh against the surviving set, so a candidate
    previously capped out by a now-dropped render-failed story can take
    its place)."""
    ready = [s for s in selected if s.render_ready]
    if len(ready) == len(selected):
        return selected
    ready_ids = {s.event_id for s in ready}
    for candidate in families:
        if len(ready) >= len(selected):
            break
        if candidate.event_id in ready_ids or not candidate.render_ready:
            continue
        if _can_add(candidate, ready):
            ready.append(candidate)
            ready_ids.add(candidate.event_id)
    ready.sort(key=lambda s: s.decision_value, reverse=True)
    return ready


def select_daily_stories(
    candidates: list[dict],
    *,
    generic_terms: set[str],
    known_entities: set[str] | None = None,
    alert_triggers: AlertTriggersConfig | None = None,
    prior_stories: list[dict] | None = None,
    max_total: int = MAX_TOTAL_STORIES,
) -> SelectionResult:
    """candidates must already be qualified (score/category/URL-validated)
    and sorted by final_score descending -- this function only suppresses
    proven repeats, clusters, covers, and caps; it never re-derives
    eligibility. `prior_stories`/`known_entities`/`alert_triggers` are
    optional so callers that don't need cross-day novelty (e.g. isolated
    unit tests of clustering/coverage) can omit them; step 0 is then a
    no-op."""
    update_notes: dict[str, str] = {}
    if prior_stories:
        candidates, update_notes = _apply_cross_day_novelty(
            candidates,
            prior_stories,
            generic_terms=generic_terms,
            known_entities=known_entities or set(),
            alert_triggers=alert_triggers,
        )

    families = group_story_families(candidates, generic_terms=generic_terms)
    for family in families:
        note = update_notes.get(family.primary["event_id"])
        if note:
            family.update_note = note
    selected: list[Story] = []

    # Step 3: cross-category coverage -- one family per required category,
    # the highest-scoring qualified family in that category, only if one
    # exists. Never invents or pads.
    for category in REQUIRED_COVERAGE_CATEGORIES:
        pick = next((f for f in families if f.category == category and f not in selected), None)
        if pick is not None and _can_add(pick, selected):
            selected.append(pick)

    # Step 4: score-based fill of remaining positions.
    for family in families:
        if len(selected) >= max_total:
            break
        if family in selected:
            continue
        if _can_add(family, selected):
            selected.append(family)

    # "At least two non-AI stories whenever four or more are selected":
    # only swaps in a family that is *already validly addable* -- never
    # bypasses the AI/domain caps, and never pads with something that
    # wasn't already a qualified, capped-compliant candidate.
    selected.sort(key=lambda s: s.decision_value, reverse=True)
    if len(selected) >= MIN_TOTAL_STORIES:
        non_ai_count = sum(1 for s in selected if not _is_ai(s))
        while non_ai_count < MIN_NON_AI_WHEN_FOUR_OR_MORE:
            worst_ai_idx = next(
                (i for i in range(len(selected) - 1, -1, -1) if _is_ai(selected[i])), None
            )
            if worst_ai_idx is None:
                break
            without_worst = selected[:worst_ai_idx] + selected[worst_ai_idx + 1 :]
            replacement = next(
                (f for f in families if f not in selected and not _is_ai(f) and _can_add(f, without_worst)), None
            )
            if replacement is None:
                break
            selected[worst_ai_idx] = replacement
            non_ai_count += 1
        selected.sort(key=lambda s: s.decision_value, reverse=True)

    selected = _apply_render_readiness_gate(selected, families)
    top_stories, more_to_know = _finalize_top_and_more(selected, max_total)
    return SelectionResult(
        top_stories=top_stories,
        more_to_know=more_to_know,
        qualified_family_count=len(families),
        distinct_publisher_families=len({f.domain for f in families if f.domain}),
    )
