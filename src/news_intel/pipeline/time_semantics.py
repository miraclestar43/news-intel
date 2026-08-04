"""Gate A remediation Phase 2: resolves a raw item's source-published time
into a validated event anchor, separate from ingestion time.

- published_anchor_at: the source/event's own published time, used for
  digest/trend/alert eligibility windows. Set once, at event creation, and
  never reset by a later re-fetch of the same content (a feed's own
  "updated" timestamp alone must never make old content look current).
- archive_only: true when the founding item had no usable published time
  (undated, or implausibly far in the future -- fail closed rather than
  trust a malformed clock). Archive-only events are excluded from
  digest/trend eligibility regardless of when they were ingested.
- content_changed_at: bumped on every later NEW/CONTENT_CHANGED item that
  merges into an existing event -- this is what lets a genuinely material
  update to an old/archive-only event become newly eligible, without
  disturbing the event's original published_anchor_at.

first_seen_at/last_updated_at (on the events table) remain pure ingestion
bookkeeping used only for clustering's own candidate-window lookup
(pipeline/cluster.py's _candidate_events) -- they are never used for
digest/trend eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from news_intel.settings import TimeSemanticsPolicy


@dataclass(frozen=True)
class ResolvedAnchor:
    published_anchor_at: datetime | None
    archive_only: bool


def resolve_anchor(
    published_at: datetime | None, *, now: datetime, policy: TimeSemanticsPolicy
) -> ResolvedAnchor:
    """Fails closed: anything that isn't a plausible past-or-present source
    timestamp becomes archive-only with no fabricated anchor, rather than
    silently defaulting to "now" (which is what let two-year-old backfilled
    articles masquerade as breaking news in Gate A)."""
    if published_at is None:
        return ResolvedAnchor(None, policy.archive_only_when_undated)

    tolerance = timedelta(minutes=policy.future_skew_tolerance_minutes)
    if published_at > now + tolerance:
        return ResolvedAnchor(None, True)

    return ResolvedAnchor(published_at, False)


def eligibility_at(row) -> datetime | None:
    """The single validated instant used for daily/weekly/trend window
    membership: a later genuine content update takes precedence over the
    original published time, but ingestion (first_seen_at/last_updated_at)
    never enters into it."""
    value = row["content_changed_at"] or row["published_anchor_at"]
    return datetime.fromisoformat(value) if value else None
