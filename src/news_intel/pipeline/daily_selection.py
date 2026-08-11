"""Daily-digest story selection (2026-08-11 content-quality repair, first
real production digest): story-family deduplication and coverage-aware
selection, applied to the SAME already-qualified candidate pool
(`event_repo.qualified_events_since`, unchanged threshold) that
jobs/daily.py has always used -- this module never lowers or bypasses
qualification, it only decides which qualified events become one digest
slot and in what order.

Root cause of the 2026-08-10 digest defects (see the accompanying incident
report): (1) two OpenAI "Daybreak" blog posts published the same day were
never same-EVENT-clustered (pipeline/cluster.py correctly did not merge
them -- their titles and extracted entities genuinely don't overlap enough
to be the *same article*), but they are clearly the *same announcement
family* for digest purposes; (2) the old select_diverse_events() only
capped by *origin_family* (e.g. "openai.com" vs "www.techmeme.com" as one
family), not by displayed source domain, and had no category-coverage or
per-topic cap logic at all -- it filled purely by score, and every
qualified event that day happened to be AI & Semiconductors.

Selection order (matches the incident report's spec exactly):
  1. Qualification and source validation -- already done by the caller
     (qualified_events_since + jobs/daily.py's own category/URL checks).
  2. Story-level clustering/deduplication -- group_story_families().
  3. Cross-category coverage -- _apply_coverage() picks one family per
     required category, from the highest-scoring qualified family in that
     category, if one exists.
  4. Score-based filling of remaining positions -- _fill_remaining(),
     respecting the AI-family cap and the per-source-domain cap.
  5. Final rendering validation -- the caller (jobs/daily.py) already
     re-validates category/URL/length before ever contacting Telegram;
     this module's caps are enforced constructively, not just checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

MAX_TOP_STORIES = 3
MAX_TOTAL_STORIES = 8
MIN_TOTAL_STORIES = 4
MAX_AI_STORIES = 2
MAX_STORIES_PER_DOMAIN = 2
MAX_TOP_STORIES_PER_DOMAIN = 1
MIN_NON_AI_WHEN_FOUR_OR_MORE = 2

TECHNOLOGY_FAMILY_CATEGORIES = {"AI & Semiconductors"}
REQUIRED_COVERAGE_CATEGORIES = ("Markets & Economy", "Politics", "US / China / World")

_CAPITALIZED_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{3,}\b")


@dataclass
class Story:
    """One digest slot: a primary event dict (event_repo.event_as_digest_dict
    shape) plus, if a story family had more than one member, up to one
    additional linked source kept as a secondary citation."""
    primary: dict
    secondary: dict | None = None

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
        return _domain_of(self.primary)


def _domain_of(item: dict) -> str:
    sources = item.get("sources") or []
    if not sources or not sources[0].get("url"):
        return ""
    return urlparse(sources[0]["url"]).netloc.lower()


def _pt_date(item: dict, tz: ZoneInfo) -> str | None:
    sources = item.get("sources") or []
    if not sources or not sources[0].get("published_at"):
        return None
    try:
        dt = datetime.fromisoformat(sources[0]["published_at"])
    except ValueError:
        return None
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def _capitalized_tokens(item: dict, generic_terms: set[str]) -> set[str]:
    text = f"{item.get('title', '')} {item.get('excerpt', '')}"
    tokens = set(_CAPITALIZED_TOKEN_RE.findall(text))
    lowered_generic = {t.lower() for t in generic_terms}
    return {t for t in tokens if t.lower() not in lowered_generic}


def _same_story_family(a: dict, b: dict, *, generic_terms: set[str], tz: ZoneInfo) -> bool:
    """Same announcement/program, not necessarily the same article. Both
    conditions are same-source AND same-Pacific-day (a real publisher
    rarely posts two genuinely unrelated stories sharing a distinctive
    proper noun on the same day) -- distinguishing this from
    pipeline/trends.py's weekly cross-day trend matching, which has no
    same-day/same-source requirement and a looser trigram-based test."""
    if _domain_of(a) != _domain_of(b) or not _domain_of(a):
        return False
    date_a, date_b = _pt_date(a, tz), _pt_date(b, tz)
    if date_a != date_b or date_a is None:
        return False
    tokens_a = _capitalized_tokens(a, generic_terms)
    tokens_b = _capitalized_tokens(b, generic_terms)
    return bool(tokens_a & tokens_b)


def group_story_families(
    candidates: list[dict], *, generic_terms: set[str], timezone: str = "America/Los_Angeles"
) -> list[Story]:
    """Complete-link grouping (mirrors pipeline/trends.py's transitive-
    bridging safeguard): a candidate only joins an existing family if it
    matches *every* current member, never just the first. Input must
    already be sorted by score descending; the highest-scoring member of
    each family becomes primary, the next-highest becomes the sole
    secondary citation (a family is a digest slot, not an unbounded list)."""
    tz = ZoneInfo(timezone)
    groups: list[list[dict]] = []
    for item in candidates:
        target = None
        for group in groups:
            if all(_same_story_family(item, member, generic_terms=generic_terms, tz=tz) for member in group):
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
    """AI-family cap and the *total* per-domain cap -- both apply
    regardless of eventual top/more-to-know placement, since those are
    decided by final score rank after every position is filled (see
    select_daily_stories). The stricter "at most one per domain among the
    eventual top stories" rule is enforced separately, after ranking, since
    which stories end up in the top-3 isn't known until selection (which
    can include coverage picks) is complete."""
    ai_count = sum(1 for s in selected if _is_ai(s))
    if _is_ai(story) and ai_count >= MAX_AI_STORIES:
        return False
    if story.domain and _domain_counts(selected).get(story.domain, 0) >= MAX_STORIES_PER_DOMAIN:
        return False
    return True


def _enforce_top_domain_cap(selected: list[Story]) -> list[Story]:
    """Post-rank pass: at most MAX_TOP_STORIES_PER_DOMAIN stories from the
    same domain may occupy the top MAX_TOP_STORIES positions. If violated,
    demote the lower-scored offender below the top cut by swapping it with
    the highest-scored story currently below the cut whose domain isn't
    already at the top-cap (a no-op if no such swap exists -- selected
    stays valid either way, just potentially with a rare unresolved
    domain-clustering in the top slots)."""
    changed = True
    while changed:
        changed = False
        top = selected[:MAX_TOP_STORIES]
        seen: dict[str, int] = {}
        for idx, story in enumerate(top):
            if not story.domain:
                continue
            seen[story.domain] = seen.get(story.domain, 0) + 1
            if seen[story.domain] > MAX_TOP_STORIES_PER_DOMAIN:
                for j in range(MAX_TOP_STORIES, len(selected)):
                    candidate = selected[j]
                    top_domains_without_offender = _domain_counts(top[:idx] + top[idx + 1 :])
                    other_count = top_domains_without_offender.get(candidate.domain, 0)
                    if not candidate.domain or other_count < MAX_TOP_STORIES_PER_DOMAIN:
                        selected[idx], selected[j] = selected[j], selected[idx]
                        changed = True
                        break
                break
    return selected


def select_daily_stories(
    candidates: list[dict], *, generic_terms: set[str], timezone: str = "America/Los_Angeles",
    max_total: int = MAX_TOTAL_STORIES,
) -> SelectionResult:
    """candidates must already be qualified (score/category/URL-validated)
    and sorted by final_score descending -- this function only clusters,
    covers, and caps; it never re-derives eligibility."""
    families = group_story_families(candidates, generic_terms=generic_terms, timezone=timezone)
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

    selected = _enforce_top_domain_cap(selected)

    top_stories = selected[:MAX_TOP_STORIES]
    more_to_know = selected[MAX_TOP_STORIES:max_total]
    return SelectionResult(top_stories=top_stories, more_to_know=more_to_know)
