"""Gate A remediation Phase 1.1: deterministic proof that run_collect only
processes genuinely NEW or CONTENT_CHANGED items, and that summary counters
reflect reality rather than "one increment per fetched item" (the bug that
produced 414,211 reported "new" items against 2,649 actual rows in Gate A).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState
from news_intel.jobs import collect as collect_module
from news_intel.jobs.collect import run_collect
from news_intel.settings import SourceEntry


class _ScriptedAdapter:
    """Returns the item at the front of a shared mutable script on every
    fetch() call, so each run_collect() invocation in a test can present a
    different (or identical) item to the pipeline."""

    def __init__(self, script: list[CollectedItem]):
        self._script = script

    def fetch(self, ctx, cursor):
        item = self._script[0]
        return FetchOutcome([item], "cursor-1", SourceHealthState.HEALTHY)


_POLL_INTERVAL_SECONDS = 60


def _install(config, monkeypatch, script: list[CollectedItem]) -> None:
    config.sources.sources.clear()
    config.sources.sources.append(
        SourceEntry(
            id="probe_source",
            name="Probe",
            adapter="rss",
            tier=0,
            enabled=True,
            url="https://x/1",
            poll_interval_seconds=_POLL_INTERVAL_SECONDS,
        )
    )
    monkeypatch.setattr(collect_module, "get_adapter", lambda adapter_type: _ScriptedAdapter(script))


def _item(*, title: str, excerpt: str, now: datetime) -> CollectedItem:
    return CollectedItem(
        source_id="probe_source",
        external_id="probe-1",
        canonical_url="https://good.test/probe-1",
        title=title,
        summary_excerpt=excerpt,
        language="en",
        published_at=now,
        origin_family="good.test",
    )


def _raw_items_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM raw_items").fetchone()["n"]


def _event_sources_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM event_sources").fetchone()["n"]


def test_first_ingestion_inserts_and_processes_once(config, conn, monkeypatch):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    script = [_item(title="Probe story breaks", excerpt="Initial report.", now=now)]
    _install(config, monkeypatch, script)

    before = _raw_items_count(conn)
    summary = run_collect(config, conn, now=now)
    after = _raw_items_count(conn)

    assert summary.new_raw_items == 1
    assert summary.changed_raw_items == 0
    assert summary.unchanged_raw_items == 0
    assert summary.processed_items == 1
    assert summary.new_events == 1
    assert after - before == summary.new_raw_items  # summary.new_raw_items == actual row-count delta


def test_second_identical_ingestion_is_a_pure_no_op(config, conn, monkeypatch):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    script = [_item(title="Probe story breaks", excerpt="Initial report.", now=now)]
    _install(config, monkeypatch, script)

    run_collect(config, conn, now=now)
    before = _raw_items_count(conn)
    events_before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    # Advance past the source's own poll interval so this is a genuine next
    # cycle (cadence/due-ness is covered separately in Phase 1.2 tests) --
    # content is unchanged, only the wall-clock moved forward.
    next_cycle = now + timedelta(seconds=_POLL_INTERVAL_SECONDS)
    second = run_collect(config, conn, now=next_cycle)
    after = _raw_items_count(conn)
    events_after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    assert second.new_raw_items == 0
    assert second.changed_raw_items == 0
    assert second.unchanged_raw_items == 1
    assert second.processed_items == 0  # no downstream normalize/cluster/confirm/score work
    assert second.new_events == 0
    assert second.updated_events == 0
    assert after == before  # no duplicate row
    assert events_after == events_before  # no duplicate event
    assert _event_sources_count(conn) == 1  # no duplicate event_sources link


def test_genuine_content_update_is_processed_exactly_once_then_replay_is_no_op(config, conn, monkeypatch):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    script = [_item(title="Probe story breaks", excerpt="Initial report.", now=now)]
    _install(config, monkeypatch, script)
    run_collect(config, conn, now=now)

    later = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)
    script[0] = _item(title="Probe story breaks", excerpt="Updated with confirmed casualty figures.", now=later)

    before = _raw_items_count(conn)
    changed_run = run_collect(config, conn, now=later)
    after = _raw_items_count(conn)

    assert changed_run.new_raw_items == 0
    assert changed_run.changed_raw_items == 1
    assert changed_run.unchanged_raw_items == 0
    assert changed_run.processed_items == 1
    assert after == before  # content update rewrites the existing row, never inserts a second one
    assert _event_sources_count(conn) == 1  # no duplicate link from the DELETE+recluster path

    # Replaying the now-current content on the next due cycle is again a no-op.
    next_cycle = later + timedelta(seconds=_POLL_INTERVAL_SECONDS)
    replay = run_collect(config, conn, now=next_cycle)
    assert replay.new_raw_items == 0
    assert replay.changed_raw_items == 0
    assert replay.unchanged_raw_items == 1
    assert replay.processed_items == 0
    assert _event_sources_count(conn) == 1


def test_timestamp_only_refresh_is_unchanged_not_new_or_changed(config, conn, monkeypatch):
    """A source that only refreshes its own fetched_at/published_at without
    any real content change must not be treated as new or changed content --
    content_hash intentionally excludes timestamps."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    script = [_item(title="Probe story breaks", excerpt="Initial report.", now=now)]
    _install(config, monkeypatch, script)
    run_collect(config, conn, now=now)

    later = datetime(2026, 7, 31, 15, 10, tzinfo=UTC)
    script[0] = _item(title="Probe story breaks", excerpt="Initial report.", now=later)

    refreshed = run_collect(config, conn, now=later)
    assert refreshed.new_raw_items == 0
    assert refreshed.changed_raw_items == 0
    assert refreshed.unchanged_raw_items == 1
    assert refreshed.processed_items == 0
