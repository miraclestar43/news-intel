"""Gate A remediation Phase 1.2: run_collect must only actually fetch a
source when its own configured cadence says it is due. Gate A ran every
source 288/288 times on the 5-minute scheduler tick regardless of its
PRD-assigned 10-30 minute interval; these tests prove the fix with an
injected clock stepped like the real systemd timer would step it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel import db
from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState
from news_intel.jobs import collect as collect_module
from news_intel.jobs.collect import run_collect
from news_intel.settings import SourceEntry


class _HealthScriptedAdapter:
    """Returns a canned FetchOutcome (with a fresh item so upsert semantics
    don't hide a real attempt) and records every call for assertion."""

    def __init__(self, source_id: str, calls: list[datetime], health: SourceHealthState, retry_after=None):
        self.source_id = source_id
        self.calls = calls
        self.health = health
        self.retry_after = retry_after
        self._n = 0

    def fetch(self, ctx, cursor):
        self.calls.append(ctx)
        self._n += 1
        if self.health != SourceHealthState.HEALTHY:
            return FetchOutcome([], cursor, self.health, "simulated", retry_after_seconds=self.retry_after)
        item = CollectedItem(
            source_id=self.source_id,
            external_id=f"{self.source_id}-{self._n}",
            canonical_url=f"https://good.test/{self.source_id}/{self._n}",
            title=f"{self.source_id} item {self._n}",
            summary_excerpt="body",
            language="en",
            published_at=None,
            origin_family="good.test",
        )
        return FetchOutcome([item], "cursor", SourceHealthState.HEALTHY)


def _entry(source_id: str, minutes: int, *, enabled: bool = True) -> SourceEntry:
    return SourceEntry(
        id=source_id, name=source_id, adapter="rss", tier=0, enabled=enabled,
        url=f"https://x/{source_id}", poll_interval_seconds=minutes * 60,
    )


def _attempt_counts(conn, config, monkeypatch, adapters: dict[str, _HealthScriptedAdapter], ticks: list[datetime]):
    def _get_adapter(adapter_type):
        # dispatch by the calling source's id, mirroring test_collect_job_isolation.py
        class _Dispatcher:
            def fetch(self, ctx, cursor):
                return adapters[ctx.id].fetch(ctx, cursor)
        return _Dispatcher()

    monkeypatch.setattr(collect_module, "get_adapter", _get_adapter)
    for tick in ticks:
        run_collect(config, conn, now=tick)


def test_5_10_15_30_minute_sources_are_attempted_only_on_their_own_cadence(config, conn, monkeypatch):
    config.sources.sources.clear()
    config.sources.sources.extend(
        [_entry("s5", 5), _entry("s10", 10), _entry("s15", 15), _entry("s30", 30)]
    )
    calls: dict[str, list] = {sid: [] for sid in ("s5", "s10", "s15", "s30")}
    adapters = {
        sid: _HealthScriptedAdapter(sid, calls[sid], SourceHealthState.HEALTHY) for sid in calls
    }

    start = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    # Simulate the real 5-minute systemd timer tick from t=0 through t=30 inclusive.
    ticks = [start + timedelta(minutes=m) for m in range(0, 31, 5)]
    _attempt_counts(conn, config, monkeypatch, adapters, ticks)

    assert len(calls["s5"]) == 7  # due at every tick: 0,5,10,15,20,25,30
    assert len(calls["s10"]) == 4  # 0,10,20,30
    assert len(calls["s15"]) == 3  # 0,15,30
    assert len(calls["s30"]) == 2  # 0,30


def test_disabled_source_is_never_attempted(config, conn, monkeypatch):
    config.sources.sources.clear()
    config.sources.sources.append(_entry("off_source", 5, enabled=False))
    calls: list = []
    adapters = {"off_source": _HealthScriptedAdapter("off_source", calls, SourceHealthState.HEALTHY)}

    start = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    ticks = [start + timedelta(minutes=m) for m in range(0, 21, 5)]
    _attempt_counts(conn, config, monkeypatch, adapters, ticks)

    assert calls == []
    last_summary = run_collect(config, conn, now=ticks[-1] + timedelta(minutes=5))
    assert last_summary.sources[0].action == "disabled"


def test_429_retry_after_is_honored_then_source_recovers_to_normal_cadence(config, conn, monkeypatch):
    config.sources.sources.clear()
    config.sources.sources.append(_entry("flaky", 10))
    calls: list = []
    # First attempt returns HTTP 429 with a 20-minute Retry-After (longer than
    # the configured 10-minute interval); subsequent attempts are healthy.
    adapter = _HealthScriptedAdapter("flaky", calls, SourceHealthState.DEGRADED, retry_after=1200)
    monkeypatch.setattr(
        collect_module, "get_adapter", lambda adapter_type: _RecoveringDispatcher(adapter)
    )

    start = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    run_collect(config, conn, now=start)  # attempt 1: 429, Retry-After=1200s
    assert len(calls) == 1

    # Still within the 20-minute Retry-After window -- must not be attempted.
    run_collect(config, conn, now=start + timedelta(minutes=10))
    assert len(calls) == 1

    # Retry-After has elapsed -- source becomes due again and recovers.
    adapter.health = SourceHealthState.HEALTHY
    run_collect(config, conn, now=start + timedelta(minutes=20))
    assert len(calls) == 2

    # Back to the plain configured 10-minute interval, no lingering backoff.
    run_collect(config, conn, now=start + timedelta(minutes=25))  # not yet due
    assert len(calls) == 2
    run_collect(config, conn, now=start + timedelta(minutes=30))  # due
    assert len(calls) == 3


class _RecoveringDispatcher:
    def __init__(self, adapter):
        self.adapter = adapter

    def fetch(self, ctx, cursor):
        return self.adapter.fetch(ctx, cursor)


def test_one_degraded_source_does_not_delay_a_sibling_source(config, conn, monkeypatch):
    config.sources.sources.clear()
    config.sources.sources.extend([_entry("slow_backoff", 10), _entry("healthy_sibling", 10)])
    calls = {"slow_backoff": [], "healthy_sibling": []}
    adapters = {
        "slow_backoff": _HealthScriptedAdapter("slow_backoff", calls["slow_backoff"], SourceHealthState.DEGRADED),
        "healthy_sibling": _HealthScriptedAdapter(
            "healthy_sibling", calls["healthy_sibling"], SourceHealthState.HEALTHY
        ),
    }
    start = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    ticks = [start + timedelta(minutes=m) for m in range(0, 21, 10)]
    _attempt_counts(conn, config, monkeypatch, adapters, ticks)

    # The sibling keeps its plain 10-minute cadence (3 attempts over 20
    # minutes) even though slow_backoff is exponentially backing off its own
    # schedule (attempt at t=0 -> degraded -> next due at t=20, so the t=10
    # tick is independently skipped for slow_backoff only).
    assert len(calls["healthy_sibling"]) == 3
    assert len(calls["slow_backoff"]) == 2


def test_restart_preserves_next_due_state(config, monkeypatch):
    """A process restart must not forget scheduling state -- next_due_at is
    read back from the persisted sources table, not from in-memory state."""
    config.sources.sources.clear()
    config.sources.sources.append(_entry("persisted_source", 10))
    calls: list = []
    adapters = {"persisted_source": _HealthScriptedAdapter("persisted_source", calls, SourceHealthState.HEALTHY)}

    start = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    conn1 = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(conn1)
    _attempt_counts(conn1, config, monkeypatch, adapters, [start])
    assert len(calls) == 1
    conn1.close()  # simulates process exit

    # "Restart": a brand-new connection to the same on-disk database.
    conn2 = db.connect(config.data_dir / "news-intel.db")
    _attempt_counts(conn2, config, monkeypatch, adapters, [start + timedelta(minutes=5)])
    assert len(calls) == 1  # still not due -- backoff/cadence state survived the restart

    _attempt_counts(conn2, config, monkeypatch, adapters, [start + timedelta(minutes=10)])
    assert len(calls) == 2  # due on schedule after the restart
    conn2.close()


def test_duplicate_scheduler_invocation_is_skipped_via_job_lock(config, conn):
    """Simulates an overlapping/duplicate systemd timer firing while a prior
    run is still (per its lock row) in flight -- run_collect must refuse to
    do any work rather than double-process."""
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    assert db.acquire_job_lock(conn, "collect") is True  # a run is already "in flight"

    summary = run_collect(config, conn, now=now)

    assert summary.skipped_lock is True
    assert summary.sources == []
    assert summary.fetched_items == 0
