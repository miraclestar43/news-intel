"""Breaking-alert shadow canary (jobs/alert_shadow.py): proves the shadow
observer persists every candidate/decision/hypothetical rendered message
without ever creating a genuine delivery record, and that its own new
dedup-by-state-change logging (on top of the already-tested real decision
engine) does not spam duplicate rows or miss/duplicate a finalized batch
across a simulated downtime/catch-up gap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel import alert_shadow_repo as shadow
from news_intel import db
from news_intel.jobs.alert_shadow import run_alert_shadow_check
from tests.acceptance.test_phase2_alert_scenarios import _seed_event


def _reopen(config):
    """Fresh connection to the same on-disk DB file, as a real systemd
    oneshot invocation would open -- mirrors test_alert_process_boundary.py."""
    conn = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(conn)
    return conn


def test_alert_shadow_module_never_imports_telegram():
    """Static, import-time guarantee (not just "never calls send()"): the
    shadow job module must not even import the Telegram delivery layer.
    Checked via the AST's actual import statements, not a substring match
    on the whole source (which would also match this docstring's prose)."""
    import ast
    import inspect

    import news_intel.jobs.alert_shadow as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any("delivery.telegram" in m for m in imported_modules)
    assert "TelegramSender" not in [n for n in dir(mod)]


def test_not_eligible_candidate_logged_once_would_not_send(config, conn):
    now = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-SH-1", title="Trump posted about new China tariffs",
        excerpt="A mirror account posted that Trump said tariffs on China would increase.",
        sources=[("trumpdailyposts_mirror_sh", 3, "trumpdailyposts.mirror")], now=now,
    )

    run_alert_shadow_check(config, conn, now=now)
    run_alert_shadow_check(config, conn, now=now + timedelta(minutes=5))  # unchanged state, no re-log

    rows = [r for r in shadow.all_rows(conn) if r["event_id"] == "E-SH-1"]
    assert len(rows) == 1
    assert rows[0]["action"] == "not_eligible"
    assert rows[0]["would_have_sent"] == 0
    assert rows[0]["severity"] is None


def test_s3_candidate_logged_as_would_have_sent_with_rendered_message_and_sources(config, conn):
    now = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-SH-2", title="Systemic financial instability triggers market shutdown",
        excerpt="Regulators reported systemic financial instability leading to a broad market shutdown.",
        sources=[("fed_gov_sh", 0, "federalreserve.gov")], now=now,
    )

    run_alert_shadow_check(config, conn, now=now)

    rows = [r for r in shadow.all_rows(conn) if r["event_id"] == "E-SH-2"]
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "sent"
    assert row["severity"] == "S3"
    assert row["would_have_sent"] == 1
    assert "E-SH-2" in row["hypothetical_rendered_text"]
    assert "https://x.test/E-SH-2-0" in row["source_urls_json"]
    assert "fed_gov_sh" in row["source_urls_json"]

    # No genuine delivery record was ever created by the shadow path.
    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0


def test_s2_pending_merge_only_would_have_sent_after_finalize(config, conn):
    t0 = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-SH-3", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_sh_3", 0, "bis.gov")], now=t0,
    )

    run_alert_shadow_check(config, conn, now=t0)
    rows = [r for r in shadow.all_rows(conn) if r["event_id"] == "E-SH-3"]
    assert len(rows) == 1
    assert rows[0]["action"] == "pending_merge"
    assert rows[0]["would_have_sent"] == 0
    assert rows[0]["hypothetical_rendered_text"] is not None  # preview available even pre-finalize
    message_hash = rows[0]["message_hash"]

    # Window closes on the next tick.
    run_alert_shadow_check(config, conn, now=t0 + timedelta(minutes=11))
    rows = [r for r in shadow.all_rows(conn) if r["event_id"] == "E-SH-3"]
    finalize_rows = [r for r in rows if r["run_kind"] == "merge_finalize"]
    assert len(finalize_rows) == 1
    assert finalize_rows[0]["would_have_sent"] == 1
    assert finalize_rows[0]["message_hash"] == message_hash
    assert finalize_rows[0]["action"] == "sent"

    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0


def test_two_s2_events_in_same_window_merge_into_one_shared_message(config, conn):
    t0 = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-SH-4A", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_sh_4a", 0, "bis.gov")], now=t0,
    )
    run_alert_shadow_check(config, conn, now=t0)

    _seed_event(
        conn, event_id="E-SH-4B", title="Commerce export control rule on equipment",
        excerpt="Commerce announced an export control rule on semiconductor equipment.",
        sources=[("bis_sh_4b", 0, "commerce.gov")], now=t0 + timedelta(minutes=3),
    )
    run_alert_shadow_check(config, conn, now=t0 + timedelta(minutes=3))

    run_alert_shadow_check(config, conn, now=t0 + timedelta(minutes=11))

    finalize_rows = [r for r in shadow.all_rows(conn) if r["run_kind"] == "merge_finalize"]
    event_ids = {r["event_id"] for r in finalize_rows}
    assert event_ids == {"E-SH-4A", "E-SH-4B"}
    message_hashes = {r["message_hash"] for r in finalize_rows}
    assert len(message_hashes) == 1  # one shared merged message
    assert all(r["would_have_sent"] == 1 for r in finalize_rows)
    # The merged rendered text is shared and mentions both events.
    rendered = {r["hypothetical_rendered_text"] for r in finalize_rows}
    assert len(rendered) == 1
    assert "E-SH-4A" in next(iter(rendered))
    assert "E-SH-4B" in next(iter(rendered))


def test_cooldown_suppression_logged_once_not_spammed(config, conn):
    t0 = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-SH-5", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_sh_5", 0, "bis.gov")], now=t0,
    )
    run_alert_shadow_check(config, conn, now=t0)
    run_alert_shadow_check(config, conn, now=t0 + timedelta(minutes=11))  # finalizes as sent

    later = t0 + timedelta(hours=1)
    run_alert_shadow_check(config, conn, now=later)
    run_alert_shadow_check(config, conn, now=later + timedelta(minutes=5))  # unchanged, must not re-log

    cooldown_rows = [
        r for r in shadow.all_rows(conn) if r["event_id"] == "E-SH-5" and r["action"] == "suppressed_cooldown"
    ]
    assert len(cooldown_rows) == 1
    assert cooldown_rows[0]["would_have_sent"] == 0


def test_daily_s2_budget_suppression_is_logged(config, conn):
    base = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    for i in range(4):
        now = base + timedelta(minutes=40 * i)  # clears the 30-minute spacing rule
        _seed_event(
            conn, event_id=f"E-SH-6-{i}", title=f"BIS export control rule #{i}",
            excerpt="BIS announced another export control rule on chips.",
            sources=[(f"bis_sh_6_{i}", 0, f"bis-6-{i}.gov")], now=now,
        )
        run_alert_shadow_check(config, conn, now=now)

    sent_events = {
        r["event_id"] for r in shadow.all_rows(conn)
        if r["run_kind"] == "merge_finalize" and r["would_have_sent"] == 1
    }
    assert sent_events == {"E-SH-6-0", "E-SH-6-1", "E-SH-6-2"}

    budget_rows = [
        r for r in shadow.all_rows(conn) if r["event_id"] == "E-SH-6-3" and r["action"] == "suppressed_budget"
    ]
    assert len(budget_rows) == 1
    assert budget_rows[0]["would_have_sent"] == 0
    assert shadow.would_send_count(conn) == 3  # exactly the S2 budget, matching real alert_queue behavior


def test_restart_safety_across_fresh_connections_does_not_duplicate(config):
    t0 = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)

    conn1 = _reopen(config)
    _seed_event(
        conn1, event_id="E-SH-7", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_sh_7", 0, "bis.gov")], now=t0,
    )
    run_alert_shadow_check(config, conn1, now=t0)
    conn1.close()

    conn2 = _reopen(config)
    run_alert_shadow_check(config, conn2, now=t0 + timedelta(minutes=3))  # replay within same window
    conn2.close()

    conn3 = _reopen(config)
    run_alert_shadow_check(config, conn3, now=t0 + timedelta(minutes=11))  # finalizes
    run_alert_shadow_check(config, conn3, now=t0 + timedelta(minutes=12))  # immediate re-check, no dup
    rows = shadow.all_rows(conn3)
    conn3.close()

    event_rows = [r for r in rows if r["event_id"] == "E-SH-7"]
    finalize_rows = [r for r in event_rows if r["run_kind"] == "merge_finalize"]
    assert len(finalize_rows) == 1  # not duplicated across process boundaries or a replayed tick


def test_late_catchup_after_downtime_finalizes_exactly_once_no_burst(config):
    """The shadow-checker "misses" the tick right after the merge window
    would have closed (simulated downtime) and only resumes several ticks
    later. Catching up must finalize the batch exactly once -- neither lost
    nor re-logged as 'sent' again on subsequent unchanged ticks."""
    t0 = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)

    conn = _reopen(config)
    _seed_event(
        conn, event_id="E-SH-8", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_sh_8", 0, "bis.gov")], now=t0,
    )
    run_alert_shadow_check(config, conn, now=t0)  # opens the batch
    conn.close()

    # Downtime: no ticks at t0+5 (would normally run every 5 minutes).
    # Catch-up resumes well after the 10-minute window has elapsed.
    conn = _reopen(config)
    run_alert_shadow_check(config, conn, now=t0 + timedelta(minutes=45))
    conn.close()

    conn = _reopen(config)
    run_alert_shadow_check(config, conn, now=t0 + timedelta(minutes=50))  # nothing changed
    conn.close()

    conn = _reopen(config)
    rows = [r for r in shadow.all_rows(conn) if r["event_id"] == "E-SH-8"]
    conn.close()

    finalize_rows = [r for r in rows if r["run_kind"] == "merge_finalize"]
    assert len(finalize_rows) == 1
    assert finalize_rows[0]["would_have_sent"] == 1


def test_shadow_check_never_writes_a_deliveries_row(config, conn):
    now = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    for i in range(3):
        _seed_event(
            conn, event_id=f"E-SH-9-{i}", title=f"BIS export control rule #{i}",
            excerpt="BIS announced another export control rule on chips.",
            sources=[(f"bis_sh_9_{i}", 0, f"bis-9-{i}.gov")], now=now + timedelta(minutes=40 * i),
        )
        run_alert_shadow_check(config, conn, now=now + timedelta(minutes=40 * i))
    run_alert_shadow_check(config, conn, now=now + timedelta(minutes=200))

    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0
