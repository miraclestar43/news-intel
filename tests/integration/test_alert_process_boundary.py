"""Deployment Gate A, item 2: proves the S2 pending-batch lifecycle survives
real process boundaries, not just repeated calls against one held-open
Python connection object. Each "invocation" below opens a brand new
sqlite3 connection to the same on-disk database file and closes it before
the next one starts, mirroring how systemd actually invokes
`pi-news alerts check` as a fresh oneshot process every cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel import alert_queue_repo as aq
from news_intel import db
from news_intel.jobs.alerts import finalize_expired_merge_batches, process_event_for_alerts
from tests.acceptance.test_phase2_alert_scenarios import _seed_event


def _reopen(config):
    """Simulates the next scheduled invocation: a brand new connection to
    the same on-disk database file, as a fresh process would open."""
    conn = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(conn)  # idempotent no-op once already applied
    return conn


def test_pending_batch_survives_process_exit_and_releases_exactly_once(config):
    t0 = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    # --- Invocation 1: t0, one S2 candidate opens a pending batch ---
    conn1 = _reopen(config)
    _seed_event(
        conn1, event_id="E-PB-1", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_pb_1", 0, "bis.gov")], now=t0,
    )
    row = conn1.execute("SELECT * FROM events WHERE id='E-PB-1'").fetchone()
    r1 = process_event_for_alerts(conn1, config, row, t0)
    assert r1.action == "pending_merge"
    batch_hash = r1.message_hash
    conn1.close()  # the process exits here

    # --- Invocation 2: t0+11min, a fresh scheduled invocation, no new candidate ---
    conn2 = _reopen(config)
    finalized = finalize_expired_merge_batches(config, conn2, t0 + timedelta(minutes=11))
    assert len(finalized) == 1
    batch = finalized[0]
    assert batch["message_hash"] == batch_hash
    assert batch["event_ids"] == ["E-PB-1"]
    assert "E-PB-1" in batch["rendered_text"]
    assert "bis_pb_1" in batch["rendered_text"]  # source reference preserved
    row_state = conn2.execute(
        "SELECT state, sent_at FROM alert_queue WHERE event_id='E-PB-1'"
    ).fetchone()
    assert row_state["state"] == "sent"
    assert row_state["sent_at"] is not None
    conn2.close()

    # --- Invocation 3: a later run cannot release the same batch again ---
    conn3 = _reopen(config)
    finalized_again = finalize_expired_merge_batches(config, conn3, t0 + timedelta(minutes=30))
    assert finalized_again == []
    sent_count = conn3.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-PB-1' AND state='sent'"
    ).fetchone()["n"]
    assert sent_count == 1  # not re-released, not duplicated
    conn3.close()


def test_replay_during_pending_window_does_not_duplicate_or_lose_the_event(config):
    t0 = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    # --- Invocation 1: opens the batch ---
    conn1 = _reopen(config)
    _seed_event(
        conn1, event_id="E-PB-2", title="Commerce export control rule on equipment",
        excerpt="Commerce announced an export control rule on semiconductor equipment.",
        sources=[("bis_pb_2", 0, "commerce.gov")], now=t0,
    )
    row = conn1.execute("SELECT * FROM events WHERE id='E-PB-2'").fetchone()
    r1 = process_event_for_alerts(conn1, config, row, t0)
    assert r1.action == "pending_merge"
    conn1.close()

    # --- Invocation 2: a re-triggered/replayed cycle re-evaluates the SAME
    # still-open event a few minutes later, inside the same window ---
    conn2 = _reopen(config)
    row = conn2.execute("SELECT * FROM events WHERE id='E-PB-2'").fetchone()
    r2 = process_event_for_alerts(conn2, config, row, t0 + timedelta(minutes=3))
    assert r2.action == "pending_merge"
    assert r2.message_hash == r1.message_hash
    pending_rows = conn2.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-PB-2' AND state='pending_merge'"
    ).fetchone()["n"]
    assert pending_rows == 1  # replay did not duplicate the row
    conn2.close()

    # --- Invocation 3: window closes; exactly one event id in the release ---
    conn3 = _reopen(config)
    finalized = finalize_expired_merge_batches(config, conn3, t0 + timedelta(minutes=11))
    assert len(finalized) == 1
    assert finalized[0]["event_ids"] == ["E-PB-2"]  # not duplicated in the batch
    total_rows = conn3.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-PB-2'"
    ).fetchone()["n"]
    assert total_rows == 1  # never lost, never duplicated
    conn3.close()


def test_budget_and_spacing_state_survive_process_boundary(config):
    t0 = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    # First S2, sent (via its own solo batch) in invocation 1.
    conn1 = _reopen(config)
    _seed_event(
        conn1, event_id="E-PB-3", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_pb_3", 0, "bis.gov")], now=t0,
    )
    row = conn1.execute("SELECT * FROM events WHERE id='E-PB-3'").fetchone()
    process_event_for_alerts(conn1, config, row, t0)
    conn1.close()

    # New process, well past the merge window but inside the 30-minute
    # spacing window: a second, unrelated S2 must still be held for
    # spacing -- proving last_sent_s2_excluding_event is read correctly
    # from a freshly opened connection, not cached Python state.
    conn2 = _reopen(config)
    finalize_expired_merge_batches(config, conn2, t0 + timedelta(minutes=11))
    _seed_event(
        conn2, event_id="E-PB-4", title="Commerce export control rule on equipment",
        excerpt="Commerce announced an export control rule on semiconductor equipment.",
        sources=[("bis_pb_4", 0, "commerce.gov")], now=t0 + timedelta(minutes=15),
    )
    row2 = conn2.execute("SELECT * FROM events WHERE id='E-PB-4'").fetchone()
    r2 = process_event_for_alerts(conn2, config, row2, t0 + timedelta(minutes=15))
    assert r2.action == "held_spacing"  # only 15 min after E-PB-3's send (t0+10)
    conn2.close()

    # A third process, past the 30-minute spacing window: now it proceeds.
    conn3 = _reopen(config)
    row3 = conn3.execute("SELECT * FROM events WHERE id='E-PB-4'").fetchone()
    r3 = process_event_for_alerts(conn3, config, row3, t0 + timedelta(minutes=41))
    assert r3.action == "pending_merge"
    pt_date = (t0 + timedelta(minutes=41)).astimezone(ZoneInfo(config.policy.timezone)).date()
    budget_count = aq.count_sent_s2_on_pt_date(conn3, pt_date, config.policy.timezone)
    assert budget_count == 1  # E-PB-3's send, correctly read back from disk
    conn3.close()
