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
     highest-scoring qualified family in that category, if one exists.
  4. Score-based filling of remaining positions -- respecting the AI-family
     cap and the per-publisher-family cap (pipeline/publisher_family.py:
     registrable-domain based, 2026-08-12 repair -- replaces the 2026-08-11
     exact-hostname cap, which let blogs.nvidia.com and developer.nvidia.com
     count as two different sources).
  5. Final rendering validation -- the caller (jobs/daily.py) already
     re-validates category/URL/length before ever contacting Telegram;
     this module's caps are enforced constructively, not just checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from news_intel.pipeline.publisher_family import publisher_family_of_url
from news_intel.pipeline.story_identity import detect_material_update, same_event_story
from news_intel.pipeline.summary_quality import has_complete_summary
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
    def domain(self) -> str:
        return _publisher_family_of(self.primary)


def _publisher_family_of(item: dict) -> str:
    sources = item.get("sources") or []
    if not sources or not sources[0].get("url"):
        return ""
    return publisher_family_of_url(sources[0]["url"])


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
    matches *every* current member, never just the first. Input must
    already be sorted by score descending; the highest-scoring member of
    each family becomes primary, the next-highest becomes the sole
    secondary citation (a family is a digest slot, not an unbounded list)."""
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
    stories.sort(key=lambda s: s.score, reverse=True)
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
    filtered `selected` list (already sorted by score descending). Two hard
    constraints apply only to TOP, never as a best-effort swap that can
    still fail (2026-08-13 repair -- the prior swap-based approach left two
    Techmeme-family stories in TOP when no valid swap partner existed):

      - at most MAX_TOP_STORIES_PER_DOMAIN stories from the same publisher
        family; a second same-family story moves to MORE TO KNOW (still
        subject to the digest-wide MAX_STORIES_PER_DOMAIN total, already
        enforced when `selected` was built) or is dropped if MORE TO KNOW
        is already at max_total.
      - a story needs a complete, source-supported summary sentence
        (pipeline/summary_quality.has_complete_summary) to occupy a TOP
        slot; a headline-only candidate demotes to MORE TO KNOW (no
        summary is required there) instead.

    TOP may end up with fewer than MAX_TOP_STORIES entries -- never padded
    with a story that fails either constraint."""
    top: list[Story] = []
    more: list[Story] = []
    top_domains_seen: set[str] = set()
    for story in selected:
        eligible_for_top = (
            len(top) < MAX_TOP_STORIES
            and (not story.domain or story.domain not in top_domains_seen)
            and has_complete_summary(story.primary)
        )
        if eligible_for_top:
            top.append(story)
            if story.domain:
                top_domains_seen.add(story.domain)
        elif len(top) + len(more) < max_total:
            more.append(story)
    return top, more


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
    selected.sort(key=lambda s: s.score, reverse=True)
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
        selected.sort(key=lambda s: s.score, reverse=True)

    top_stories, more_to_know = _finalize_top_and_more(selected, max_total)
    return SelectionResult(top_stories=top_stories, more_to_know=more_to_know)
