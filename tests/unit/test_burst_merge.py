"""Phase 2 completion item 1: the 10-minute S2 burst-merge coalescing
window. S3 always bypasses it; quiet-hours behavior is untouched (checked
before the merge logic ever runs); candidates outside the window fall back
to the existing 30-minute spacing rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel import alert_queue_repo as aq
from news_intel.jobs.alerts import finalize_expired_merge_batches, process_event_for_alerts
from tests.acceptance.test_phase2_alert_scenarios import _seed_event


def test_two_s2_candidates_eight_minutes_apart_consolidate_into_one_message(config, conn):
    t0 = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-BM-A", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_bm_a", 0, "bis.gov")], now=t0,
    )
    r1 = process_event_for_alerts(conn, config, conn.execute(
        "SELECT * FROM events WHERE id='E-BM-A'").fetchone(), t0)
    assert r1.action == "pending_merge"

    t1 = t0 + timedelta(minutes=8)
    _seed_event(
        conn, event_id="E-BM-B", title="Commerce export control rule on equipment",
        excerpt="Commerce announced an export control rule on semiconductor equipment.",
        sources=[("bis_bm_b", 0, "commerce.gov")], now=t1,
    )
    r2 = process_event_for_alerts(conn, config, conn.execute(
        "SELECT * FROM events WHERE id='E-BM-B'").fetchone(), t1)
    assert r2.action == "pending_merge"
    assert r2.message_hash == r1.message_hash  # joined the same batch

    # Window closes 10 minutes after the FIRST candidate (t0+10), not the second.
    finalized = finalize_expired_merge_batches(config, conn, t0 + timedelta(minutes=11))

    assert len(finalized) == 1
    batch = finalized[0]
    assert set(batch["event_ids"]) == {"E-BM-A", "E-BM-B"}
    assert "Breaking Brief" in batch["rendered_text"]
    assert "E-BM-A" in batch["rendered_text"] and "E-BM-B" in batch["rendered_text"]

    states = conn.execute(
        "SELECT event_id, state, message_hash FROM alert_queue WHERE event_id IN ('E-BM-A','E-BM-B')"
    ).fetchall()
    assert all(s["state"] == "sent" for s in states)
    assert len({s["message_hash"] for s in states}) == 1


def test_candidates_more_than_ten_minutes_apart_are_not_merged(config, conn):
    t0 = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-NOMERGE-A", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_nm_a", 0, "bis.gov")], now=t0,
    )
    r1 = process_event_for_alerts(conn, config, conn.execute(
        "SELECT * FROM events WHERE id='E-NOMERGE-A'").fetchone(), t0)
    assert r1.action == "pending_merge"

    # Arrives 15 minutes later: batch A's 10-minute window (deadline t0+10)
    # has already elapsed, so this candidate cannot join it.
    t1 = t0 + timedelta(minutes=15)
    _seed_event(
        conn, event_id="E-NOMERGE-B", title="Commerce export control rule on equipment",
        excerpt="Commerce announced an export control rule on semiconductor equipment.",
        sources=[("bis_nm_b", 0, "commerce.gov")], now=t1,
    )
    r2 = process_event_for_alerts(conn, config, conn.execute(
        "SELECT * FROM events WHERE id='E-NOMERGE-B'").fetchone(), t1)

    # Batch A finalized (as a solo alert) as a side effect of processing B;
    # B itself is now subject to the 30-minute spacing rule from A's send time
    # (t0+10), and t1 (t0+15) is only 5 minutes past that.
    row_a_state = conn.execute(
        "SELECT state, message_hash FROM alert_queue WHERE event_id='E-NOMERGE-A'"
    ).fetchone()
    assert row_a_state["state"] == "sent"
    assert r2.action == "held_spacing"
    assert r2.message_hash != row_a_state["message_hash"]


def test_s3_during_s2_merge_window_is_immediate(config, conn):
    t0 = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-MERGE-S2", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_merge_s2", 0, "bis.gov")], now=t0,
    )
    r_s2 = process_event_for_alerts(conn, config, conn.execute(
        "SELECT * FROM events WHERE id='E-MERGE-S2'").fetchone(), t0)
    assert r_s2.action == "pending_merge"  # open window, not yet finalized

    t_mid = t0 + timedelta(minutes=3)
    _seed_event(
        conn, event_id="E-MERGE-S3", title="Systemic financial instability triggers market shutdown",
        excerpt="Regulators reported systemic financial instability leading to a broad market shutdown.",
        sources=[("fed_merge_s3", 0, "federalreserve.gov")], now=t_mid,
    )
    r_s3 = process_event_for_alerts(conn, config, conn.execute(
        "SELECT * FROM events WHERE id='E-MERGE-S3'").fetchone(), t_mid)

    assert r_s3.action == "sent"  # S3 bypasses the merge window entirely

    # The S2 batch is untouched -- still open, waiting out its own window.
    row_s2_state = conn.execute(
        "SELECT state FROM alert_queue WHERE event_id='E-MERGE-S2'"
    ).fetchone()
    assert row_s2_state["state"] == "pending_merge"


def test_merged_delivery_consumes_one_message_from_s2_budget(config, conn):
    t0 = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    for i, minutes_offset in enumerate([0, 4, 8]):
        event_id = f"E-BUDGET-{i}"
        arrival = t0 + timedelta(minutes=minutes_offset)
        _seed_event(
            conn, event_id=event_id, title=f"BIS export control rule #{i}",
            excerpt="BIS announced another export control rule on chips.",
            sources=[(f"bis_budget_{i}", 0, f"bis-budget-{i}.gov")], now=arrival,
        )
        result = process_event_for_alerts(
            conn, config, conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone(), arrival
        )
        assert result.action == "pending_merge"

    finalize_expired_merge_batches(config, conn, t0 + timedelta(minutes=11))

    pt_date = t0.astimezone(ZoneInfo(config.policy.timezone)).date()
    budget_count = aq.count_sent_s2_on_pt_date(conn, pt_date, config.policy.timezone)
    assert budget_count == 1  # three merged events, one message

    sent_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE state='sent' AND severity='S2'"
    ).fetchone()["n"]
    assert sent_rows == 3  # every event id is preserved individually
