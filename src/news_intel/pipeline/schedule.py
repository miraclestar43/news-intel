"""Gate A remediation Phase 1.2: persisted per-source due-scheduling.

Pure functions over explicit inputs (the injected clock, never wall time
directly) so cadence/backoff behavior is deterministically testable. State
(next_due_at) is persisted in the `sources` table (sources_repo.py), which
is what makes restart/replay preserve it -- there is no in-memory state
here at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta

DEFAULT_MAX_BACKOFF_MULTIPLIER = 8


def is_source_due(*, next_due_at: datetime | None, now: datetime) -> bool:
    return next_due_at is None or now >= next_due_at


def compute_next_due_at(
    *,
    now: datetime,
    poll_interval_seconds: int,
    is_healthy: bool,
    consecutive_failures: int,
    retry_after_seconds: float | None,
    max_backoff_multiplier: int = DEFAULT_MAX_BACKOFF_MULTIPLIER,
) -> datetime:
    """Called after every attempt to persist when this source should next be
    attempted. A healthy outcome resets to the plain configured interval --
    backoff never lingers once a source recovers. A degraded/unavailable
    outcome honors an explicit Retry-After when the remote server gave one
    (never shorter than the source's own configured interval); otherwise it
    backs off exponentially by consecutive_failures, capped."""
    if is_healthy:
        return now + timedelta(seconds=poll_interval_seconds)

    if retry_after_seconds is not None:
        delay = max(retry_after_seconds, poll_interval_seconds)
    else:
        multiplier = min(2 ** max(consecutive_failures, 0), max_backoff_multiplier)
        delay = poll_interval_seconds * multiplier
    return now + timedelta(seconds=delay)
