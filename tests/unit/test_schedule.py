"""Gate A remediation Phase 1.2: pure clock-controlled tests for the
persisted source-due scheduler. These test the deterministic functions
directly (no DB, no wall clock) -- schedule.py never reads real time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.pipeline.schedule import compute_next_due_at, is_source_due


def test_never_attempted_source_is_always_due():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    assert is_source_due(next_due_at=None, now=now) is True


def test_source_not_due_before_next_due_at():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = now + timedelta(minutes=10)
    assert is_source_due(next_due_at=next_due, now=now) is False
    assert is_source_due(next_due_at=next_due, now=now + timedelta(minutes=9, seconds=59)) is False


def test_source_due_exactly_at_or_after_next_due_at():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = now + timedelta(minutes=10)
    assert is_source_due(next_due_at=next_due, now=next_due) is True
    assert is_source_due(next_due_at=next_due, now=next_due + timedelta(seconds=1)) is True


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


def test_healthy_outcome_resets_backoff_regardless_of_prior_failures():
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    next_due = compute_next_due_at(
        now=now,
        poll_interval_seconds=600,
        is_healthy=True,
        consecutive_failures=5,  # a prior streak of failures, now recovered
        retry_after_seconds=None,
    )
    assert next_due == now + timedelta(seconds=600)  # no lingering backoff


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
