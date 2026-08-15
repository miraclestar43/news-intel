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
  5. Summary enrichment -- attempted for every selected story regardless of
     rank; a story that already earned its slot on decision value is never
     displaced by a lower-value story just because its summary extraction
     failed (see _render_top_story's explicit "no summary available" note
     in delivery/daily_production_formatter.py).
  6. Final rendering validation -- the caller (jobs/daily.py) already
     re-validates category/URL/length before ever contacting Telegram;
     this module's caps are enforced constructively, not just checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from news_intel.pipeline.publisher_family import publisher_family_of_url
from news_intel.pipeline.render_readiness import is_render_ready
from news_intel.pipeline.story_identity import detect_material_update, same_event_story
from news_intel.settings import AlertTriggersConfig

MAX_TOP_STORIES = 3
MAX_TOTAL_STORIES = 8
MIN_TOTAL_STORIES = 4
MAX_AI_STORIES = 2
MAX_STORIES_PER_DOMAIN = 2
MAX_TOP_STORIES_PER_DOMAIN = 1
MIN_NON_AI_WHEN_FOUR_OR_MORE = 2

TECHNOLOGY_FAMILY_CATEGORIES = {"AI & Semiconductors"}
REQUIRED_COVERAGE_CATEGORIES = ("Markets & Economy", "Politics", "US / China / World")


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

    @property
    def all_stories(self) -> list[Story]:
        return self.top_stories + self.more_to_know


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

    2026-08-14 repair: summary availability is deliberately NOT a TOP-
    eligibility gate here anymore -- "summary availability must not cause a
    lower-value story to outrank a higher-value event." The 2026-08-13
    version of this rule demoted any story with a poorly-formatted excerpt
    straight to MORE TO KNOW regardless of how material it was, which is
    exactly what let a routine tutorial win a TOP slot over a genuine
    earnings beat whose Techmeme excerpt happened to be cut off mid-
    sentence. The renderer (delivery/daily_production_formatter.py) shows
    an explicit "no summary available" note for a TOP story with no
    extractable sentence, rather than silently promoting a lower-value
    story to fill the gap.

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


def _apply_render_readiness_gate(
    top: list[Story], more: list[Story], families: list[Story], max_top: int
) -> tuple[list[Story], list[Story]]:
    """2026-08-15 repair: a story that earned its TOP slot on decision value
    (see module docstring, stage 4) but for which enrichment -- primary
    source, then linked secondary source, the only two sources this
    pipeline has for it -- still produces no complete "what happened"
    sentence must not occupy that slot with a placeholder. It is demoted
    (never dropped outright -- MORE TO KNOW never required a full account,
    just a title and a truthful link, so it can still inform the reader
    there). The vacated slot is backfilled, in order: first from MORE TO
    KNOW's own already-selected, already-cap-compliant, decision-value-
    ordered stories; then from any other qualified family that lost an
    earlier cap competition (e.g. the AI-family cap) but would validly fit
    now that a slot is open -- re-checked against _can_add so this can
    never bypass the AI-family or per-publisher-family caps. Ordering
    itself was already fixed before this gate runs -- this only ever swaps
    a render-failed story for a render-ready one, never reorders on any
    other basis."""
    ready_top: list[Story] = []
    demoted: list[Story] = []
    for story in top:
        if story.render_ready:
            ready_top.append(story)
        else:
            demoted.append(story)
    if not demoted:
        return top, more

    remaining_more = list(more)
    already_selected_ids = {s.event_id for s in top} | {s.event_id for s in more}
    already_selected_pool = [s for s in remaining_more if s.render_ready]
    new_pool = [f for f in families if f.event_id not in already_selected_ids and f.render_ready]
    top_domains = {s.domain for s in ready_top if s.domain}

    def _try_promote(candidate: Story, *, needs_cap_check: bool) -> bool:
        if len(ready_top) >= max_top:
            return False
        if candidate.domain and candidate.domain in top_domains:
            return False
        if needs_cap_check and not _can_add(candidate, ready_top + remaining_more):
            return False
        ready_top.append(candidate)
        if candidate.domain:
            top_domains.add(candidate.domain)
        return True

    for candidate in already_selected_pool:
        if _try_promote(candidate, needs_cap_check=False) and candidate in remaining_more:
            remaining_more.remove(candidate)
    for candidate in new_pool:
        _try_promote(candidate, needs_cap_check=True)

    return ready_top, demoted + remaining_more


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

    top_stories, more_to_know = _finalize_top_and_more(selected, max_total)
    top_stories, more_to_know = _apply_render_readiness_gate(top_stories, more_to_know, families, MAX_TOP_STORIES)
    return SelectionResult(top_stories=top_stories, more_to_know=more_to_know)
