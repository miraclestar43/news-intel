"""Deterministic digest selection policy (Phase 1 hardening).

Turns a score-ranked pool of qualified events into the final digest list
without letting one prolific origin family (e.g. a single company's
newsroom/blog posting many stories in one window) crowd out everything else,
while never padding below the quality bar and never hiding several
genuinely major same-category events behind a hard cap.

Order of operations:
1. Greedy, score-ordered selection capped at `max_per_family` events per
   origin family. Skipped (over-capped) candidates are deferred, not
   dropped.
2. If the family cap left us under `min_events` -- i.e. there were not
   enough qualified alternatives to fill the digest without repeating a
   family -- backfill from the deferred pool by score so the digest is
   never thinner than the natural qualified count (up to min_events).
3. Soft coverage top-up: if spare capacity remains (< max_events) and the
   qualified pool contains an AI/semiconductor/technology or markets/economy
   event that didn't make the cut, add it. This only ever *adds* using
   spare capacity -- it never evicts an already-selected event, so it can
   never hide a genuinely major same-category story.
4. Re-sort the final selection by score so Top 3 stays primarily ordered by
   importance, then split into the top3/quick-brief tiers by the existing
   score thresholds.
"""

from __future__ import annotations

import sqlite3

AI_SEMI_TECH_CATEGORIES = {"AI & Semiconductors", "Technology"}
MARKETS_CATEGORIES = {"Markets & Economy"}


def _family_of(row: sqlite3.Row) -> str:
    return row["primary_origin_family"] or f"event:{row['id']}"


def _category_of(row: sqlite3.Row) -> str:
    return row["category"] or "uncategorized"


def _diverse_pool(
    qualified_rows: list[sqlite3.Row], *, max_per_family: int, min_events: int, max_events: int
) -> list[sqlite3.Row]:
    family_counts: dict[str, int] = {}
    selected: list[sqlite3.Row] = []
    deferred: list[sqlite3.Row] = []

    for row in qualified_rows:
        family = _family_of(row)
        if family_counts.get(family, 0) < max_per_family:
            selected.append(row)
            family_counts[family] = family_counts.get(family, 0) + 1
        else:
            deferred.append(row)

    natural_count = min(min_events, len(qualified_rows))
    if len(selected) < natural_count:
        need = natural_count - len(selected)
        selected.extend(deferred[:need])
        deferred = deferred[need:]

    return selected[:max_events], deferred


def _top_up_coverage(
    selected: list[sqlite3.Row],
    qualified_rows: list[sqlite3.Row],
    *,
    max_events: int,
) -> list[sqlite3.Row]:
    selected_ids = {row["id"] for row in selected}

    def count_in(categories: set[str]) -> int:
        return sum(1 for row in selected if _category_of(row) in categories)

    def find_candidate(categories: set[str]):
        return next(
            (r for r in qualified_rows if r["id"] not in selected_ids and _category_of(r) in categories),
            None,
        )

    while count_in(AI_SEMI_TECH_CATEGORIES) < 2 and len(selected) < max_events:
        candidate = find_candidate(AI_SEMI_TECH_CATEGORIES)
        if candidate is None:
            break
        selected.append(candidate)
        selected_ids.add(candidate["id"])

    while count_in(MARKETS_CATEGORIES) < 1 and len(selected) < max_events:
        candidate = find_candidate(MARKETS_CATEGORIES)
        if candidate is None:
            break
        selected.append(candidate)
        selected_ids.add(candidate["id"])

    return selected


def select_diverse_events(
    qualified_rows: list[sqlite3.Row],
    *,
    max_per_family: int,
    min_events: int,
    max_events: int,
    top3_threshold: float,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Returns (top3_rows, quick_brief_rows), both drawn from a
    family-capped, coverage-aware subset of `qualified_rows` (which must
    already be sorted by final_score DESC)."""
    if not qualified_rows:
        return [], []

    selected, deferred = _diverse_pool(
        qualified_rows, max_per_family=max_per_family, min_events=min_events, max_events=max_events
    )
    selected = _top_up_coverage(selected, qualified_rows, max_events=max_events)
    selected.sort(key=lambda r: r["final_score"], reverse=True)

    top3 = [r for r in selected if r["final_score"] >= top3_threshold][:3]
    top3_ids = {r["id"] for r in top3}
    remaining_budget = max(0, max_events - len(top3))
    quick = [r for r in selected if r["id"] not in top3_ids][:remaining_budget]
    return top3, quick
