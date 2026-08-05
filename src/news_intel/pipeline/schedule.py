"""Gate A remediation Phase 1.2 (Retry 1 Blocker 1 fix): persisted
per-source due-scheduling on a fixed lattice, not actual-attempt-time.

Pure functions over explicit inputs (the injected clock, never wall time
directly) so cadence/backoff behavior is deterministically testable. State
(next_due_at) is persisted in the `sources` table (sources_repo.py), which
is what makes restart/replay preserve it -- there is no in-memory state
here at all.

Retry 1 found that computing next_due_at as `actual_attempt_time +
interval` lets a source whose interval matches (or nearly matches) the
5-minute scheduler tick alternate between attempted/skipped indefinitely:
any small jitter in exactly when the tick fires relative to the previous
*actual* attempt gets baked into the next boundary, so the schedule can
permanently drift off the tick grid. The fix: always advance from the
previous *scheduled* slot (a fixed lattice, `previous_due_at + interval`,
repeated as many times as needed to reach a future slot), never from when
the attempt actually happened, plus a small bounded grace window so
sub-second process-start jitter can't push a tick just past its own exact
boundary. A large gap (restart, downtime, backoff) is caught up to the
first future slot in one step -- never a burst of repeated immediate
attempts for each missed slot.
"""

from __future__ import annotations

from datetime import datetime, timedelta

DEFAULT_MAX_BACKOFF_MULTIPLIER = 8

# Absorbs sub-second/small process-start jitter in exactly when a tick
# fires relative to its scheduled slot -- without this, a source whose
# interval equals the scheduler tick period can skip roughly half its
# opportunities purely from noise at the exact boundary (Retry 1 finding).
# Small relative to any configured interval (>=300s), so it never
# meaningfully shortens the configured cadence.
DEFAULT_BOUNDARY_GRACE_SECONDS = 2.0


def is_source_due(
    *, next_due_at: datetime | None, now: datetime, grace_seconds: float = DEFAULT_BOUNDARY_GRACE_SECONDS
) -> bool:
    if next_due_at is None:
        return True
    return now + timedelta(seconds=grace_seconds) >= next_due_at


def compute_next_regular_due_at(
    *, previous_due_at: datetime | None, now: datetime, poll_interval_seconds: int
) -> datetime:
    """The fixed-lattice advance: always `previous_due_at + interval`
    (never `now + interval`), repeated until the result is in the future.
    A never-before-scheduled source (previous_due_at is None) bootstraps
    its lattice from the current attempt -- there is no earlier slot to
    anchor to, so this one attempt defines the grid going forward. This is
    the single explicitly-defined initial-boundary exception; every
    subsequent slot is a fixed offset from it, never from actual attempt
    time, so ordinary jitter can never accumulate into drift."""
    interval = timedelta(seconds=poll_interval_seconds)
    next_due = previous_due_at + interval if previous_due_at is not None else now + interval
    while next_due <= now:
        next_due += interval
    return next_due


def compute_next_due_at(
    *,
    now: datetime,
    poll_interval_seconds: int,
    is_healthy: bool,
    consecutive_failures: int,
    retry_after_seconds: float | None,
    previous_due_at: datetime | None = None,
    max_backoff_multiplier: int = DEFAULT_MAX_BACKOFF_MULTIPLIER,
) -> datetime:
    """Called after every attempt to persist when this source should next be
    attempted. A healthy outcome advances the regular lattice by exactly
    one interval from its previous scheduled slot -- backoff never lingers
    once a source recovers, and it resumes on the regular schedule rather
    than restarting the lattice from an arbitrary point. A
    degraded/unavailable outcome honors an explicit Retry-After when the
    remote server gave one (never shorter than the source's own configured
    interval); otherwise it backs off exponentially by consecutive_failures,
    capped. Backoff is intentionally computed from `now` (suppression
    while active), not the lattice -- once it clears, the next healthy
    attempt's regular-lattice advance (from whatever next_due_at backoff
    left behind) catches up to the next future slot in one step, never a
    hammering burst of missed intervals."""
    if is_healthy:
        return compute_next_regular_due_at(
            previous_due_at=previous_due_at, now=now, poll_interval_seconds=poll_interval_seconds
        )

    if retry_after_seconds is not None:
        delay = max(retry_after_seconds, poll_interval_seconds)
    else:
        multiplier = min(2 ** max(consecutive_failures, 0), max_backoff_multiplier)
        delay = poll_interval_seconds * multiplier
    return now + timedelta(seconds=delay)
