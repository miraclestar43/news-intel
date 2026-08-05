"""Gate A remediation Phase 1.2 / Retry 1 Blocker 1 fix: pure clock-
controlled tests for the persisted source-due scheduler. These test the
deterministic functions directly (no DB, no wall clock) -- schedule.py
never reads real time.

Retry 1 found that computing next_due_at from actual-attempt-time let a
source whose interval matched the 5-minute scheduler tick alternate
attempted/skipped indefinitely (175/284 observed vs. ~283 expected). The
fix is a fixed lattice (previous *scheduled* slot + interval, never actual
attempt time) plus a small bounded grace window for sub-second jitter.
These tests prove the fix directly and guard against regressing back to
actual-time-based scheduling.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.pipeline.schedule import (
    DEFAULT_BOUNDARY_GRACE_SECONDS,
    compute_next_due_at,
    compute_next_regular_due_at,
    is_source_due,
)


def test_never_attempted_source_is_always_due():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    assert is_source_due(next_due_at=None, now=now) is True


def test_source_not_due_well_before_next_due_at():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = now + timedelta(minutes=10)
    assert is_source_due(next_due_at=next_due, now=now) is False
    # Well outside the grace window -- not due.
    assert is_source_due(next_due_at=next_due, now=next_due - timedelta(seconds=30)) is False


def test_source_due_within_the_small_boundary_grace_window():
    """A tick landing a fraction of a second before the exact scheduled
    instant (process-start jitter) must still count as due -- this is what
    stops a source whose interval equals the tick period from randomly
    skipping roughly half its opportunities."""
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = now + timedelta(minutes=10)
    just_before = next_due - timedelta(milliseconds=500)
    assert is_source_due(next_due_at=next_due, now=just_before) is True

    # Just outside the grace window -- correctly not due.
    outside_grace = next_due - timedelta(seconds=DEFAULT_BOUNDARY_GRACE_SECONDS + 1)
    assert is_source_due(next_due_at=next_due, now=outside_grace) is False


def test_source_due_exactly_at_or_after_next_due_at():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = now + timedelta(minutes=10)
    assert is_source_due(next_due_at=next_due, now=next_due) is True
    assert is_source_due(next_due_at=next_due, now=next_due + timedelta(seconds=1)) is True


def test_first_ever_attempt_bootstraps_the_lattice_from_now():
    """The single explicitly-defined initial-boundary exception: with no
    previous scheduled slot, the lattice originates at the first attempt."""
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = compute_next_regular_due_at(previous_due_at=None, now=now, poll_interval_seconds=300)
    assert next_due == now + timedelta(seconds=300)


def test_regular_lattice_advances_from_previous_scheduled_slot_not_actual_time():
    """The core Blocker 1 fix: next_due_at is previous_due_at + interval,
    never now + interval -- so a late attempt does not shift the grid."""
    scheduled = datetime(2026, 7, 31, 12, 5, 0, tzinfo=UTC)  # the slot that was due
    late_actual_attempt = scheduled + timedelta(seconds=45)  # attempt ran a bit late
    next_due = compute_next_regular_due_at(
        previous_due_at=scheduled, now=late_actual_attempt, poll_interval_seconds=300
    )
    # Anchored to the *scheduled* slot (12:05) + interval, not to the late
    # actual attempt time (12:05:45) + interval.
    assert next_due == scheduled + timedelta(seconds=300)
    assert next_due != late_actual_attempt + timedelta(seconds=300)


def test_accumulated_sub_second_jitter_never_drifts_the_lattice():
    """Simulates many ticks, each firing a small, varying amount before its
    exact scheduled instant (realistic process-start jitter) -- the lattice
    must stay locked to the original grid throughout, never accumulating
    drift the way actual-attempt-time-based scheduling did."""
    interval = 300
    origin = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    previous_due = origin + timedelta(seconds=interval)  # first slot already scheduled

    jitters_ms = [50, -20, 300, -450, 10, 999, -100, 0, 1800, -1999]  # ms, some early some "late" within tick
    for i, jitter_ms in enumerate(jitters_ms, start=1):
        expected_slot = origin + timedelta(seconds=interval * (i + 1))
        actual_tick_time = expected_slot + timedelta(milliseconds=jitter_ms)
        assert is_source_due(next_due_at=previous_due, now=actual_tick_time) is True, (
            f"tick {i} with jitter {jitter_ms}ms should have been due"
        )
        previous_due = compute_next_regular_due_at(
            previous_due_at=previous_due, now=actual_tick_time, poll_interval_seconds=interval
        )
        assert previous_due == expected_slot + timedelta(seconds=interval), (
            f"lattice drifted after tick {i}: got {previous_due}, expected exactly one interval past the fixed grid"
        )


def test_regular_lattice_holds_for_5_10_15_30_minute_intervals():
    origin = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    for minutes in (5, 10, 15, 30):
        interval = minutes * 60
        due = origin + timedelta(seconds=interval)
        for _ in range(6):
            attempt_time = due  # exactly on schedule
            assert is_source_due(next_due_at=due, now=attempt_time) is True
            due = compute_next_regular_due_at(previous_due_at=due, now=attempt_time, poll_interval_seconds=interval)


def test_late_scheduler_tick_advances_to_next_future_slot_with_a_single_catch_up():
    """A tick that fires notably late (e.g. the scheduler itself was
    delayed) must still result in exactly one attempt, with next_due_at
    landing on the correct future slot -- not the slot immediately after
    the one that was missed, and not multiple slots processed at once."""
    scheduled = datetime(2026, 7, 31, 12, 5, 0, tzinfo=UTC)
    very_late_attempt = scheduled + timedelta(seconds=450)  # 1.5x the 300s interval late
    next_due = compute_next_regular_due_at(
        previous_due_at=scheduled, now=very_late_attempt, poll_interval_seconds=300
    )
    # scheduled+300=12:10:00 is still <= the late attempt (12:12:30), so it
    # must keep advancing to the first slot actually in the future.
    assert next_due > very_late_attempt
    assert next_due == datetime(2026, 7, 31, 12, 15, 0, tzinfo=UTC)


def test_downtime_does_not_perform_a_catch_up_burst():
    """A source down for hours (restart, extended backoff) must, upon its
    next attempt, jump straight to the next future slot in one step --
    never require the caller to loop through every missed interval."""
    scheduled = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    three_hours_later = scheduled + timedelta(hours=3)
    next_due = compute_next_regular_due_at(
        previous_due_at=scheduled, now=three_hours_later, poll_interval_seconds=300
    )
    # Exactly one step past "now", not 36 accumulated 5-minute increments.
    assert next_due > three_hours_later
    assert next_due - three_hours_later <= timedelta(seconds=300)


def test_healthy_outcome_schedules_plain_configured_interval_for_5_10_15_30_minute_sources():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    for minutes in (5, 10, 15, 30):
        next_due = compute_next_due_at(
            now=now,
            poll_interval_seconds=minutes * 60,
            is_healthy=True,
            consecutive_failures=0,
            retry_after_seconds=None,
        )
        assert next_due == now + timedelta(minutes=minutes)


def test_healthy_outcome_resumes_regular_lattice_from_previous_due_at():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    previous_due = now  # this attempt happened exactly on its scheduled slot
    next_due = compute_next_due_at(
        now=now,
        poll_interval_seconds=600,
        is_healthy=True,
        consecutive_failures=5,  # a prior streak of failures, now recovered
        retry_after_seconds=None,
        previous_due_at=previous_due,
    )
    assert next_due == now + timedelta(seconds=600)  # no lingering backoff, resumes regular cadence


def test_degraded_outcome_without_retry_after_backs_off_exponentially():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    poll_interval = 600
    expected_multipliers = {1: 2, 2: 4, 3: 8}
    for failures, multiplier in expected_multipliers.items():
        next_due = compute_next_due_at(
            now=now,
            poll_interval_seconds=poll_interval,
            is_healthy=False,
            consecutive_failures=failures,
            retry_after_seconds=None,
        )
        assert next_due == now + timedelta(seconds=poll_interval * multiplier)


def test_degraded_outcome_backoff_is_capped():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    poll_interval = 600
    uncapped = compute_next_due_at(
        now=now,
        poll_interval_seconds=poll_interval,
        is_healthy=False,
        consecutive_failures=10,  # 2**10 would be way past the cap
        retry_after_seconds=None,
        max_backoff_multiplier=8,
    )
    assert uncapped == now + timedelta(seconds=poll_interval * 8)


def test_429_with_retry_after_is_honored_over_backoff():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    # consecutive_failures=1 with poll_interval=600 would give a 1200s
    # exponential backoff; a longer explicit Retry-After must win instead.
    next_due = compute_next_due_at(
        now=now,
        poll_interval_seconds=600,
        is_healthy=False,
        consecutive_failures=1,
        retry_after_seconds=1500,
    )
    assert next_due == now + timedelta(seconds=1500)


def test_retry_after_never_shorter_than_configured_interval():
    """A server sending a tiny Retry-After must not override the source's
    own configured floor -- otherwise a misbehaving/malicious server could
    force hammering well below the PRD-assigned cadence."""
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = compute_next_due_at(
        now=now,
        poll_interval_seconds=600,
        is_healthy=False,
        consecutive_failures=1,
        retry_after_seconds=5,
    )
    assert next_due == now + timedelta(seconds=600)


def test_recovery_after_backoff_resumes_on_a_future_regular_schedule_without_hammering():
    """After backoff clears, the next healthy attempt's regular-lattice
    advance must catch up from wherever backoff left next_due_at to the
    next future slot in one step -- not hammer every interval that elapsed
    during the outage."""
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    poll_interval = 300
    # Two failures pushed next_due_at out to now+1200s (backoff path).
    backoff_due = compute_next_due_at(
        now=now, poll_interval_seconds=poll_interval, is_healthy=False,
        consecutive_failures=2, retry_after_seconds=None,
    )
    assert backoff_due == now + timedelta(seconds=1200)

    # A long additional gap passes before the source is actually attempted
    # again (e.g. the whole pilot was paused) -- now is far past backoff_due.
    much_later = backoff_due + timedelta(hours=2)
    recovered_due = compute_next_due_at(
        now=much_later, poll_interval_seconds=poll_interval, is_healthy=True,
        consecutive_failures=0, retry_after_seconds=None, previous_due_at=backoff_due,
    )
    # Exactly one step past "now" -- not a burst of ~24 missed 5-minute slots.
    assert recovered_due > much_later
    assert recovered_due - much_later <= timedelta(seconds=poll_interval)
