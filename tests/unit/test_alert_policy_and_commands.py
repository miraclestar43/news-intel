from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from news_intel.delivery import commands
from news_intel.jobs.alerts import process_event_for_alerts, release_held_alerts
from news_intel.pipeline.alert_policy import is_quiet_hours, next_quiet_hours_end
from news_intel.pipeline.alert_triggers import match_trigger, matches_trigger_id
from news_intel.settings import QuietHours
from tests.acceptance.test_phase2_alert_scenarios import _seed_event

PT = ZoneInfo("America/Los_Angeles")
QH = QuietHours(start="23:30", end="06:30")


def test_is_quiet_hours_wraps_midnight():
    assert is_quiet_hours(time(23, 45), QH) is True
    assert is_quiet_hours(time(1, 0), QH) is True
    assert is_quiet_hours(time(6, 29), QH) is True
    assert is_quiet_hours(time(6, 30), QH) is False
    assert is_quiet_hours(time(12, 0), QH) is False


def test_next_quiet_hours_end_same_day_vs_next_day():
    early = datetime(2026, 1, 15, 1, 0, tzinfo=PT)
    assert next_quiet_hours_end(early, QH) == datetime(2026, 1, 15, 6, 30, tzinfo=PT)

    late = datetime(2026, 1, 15, 23, 45, tzinfo=PT)
    assert next_quiet_hours_end(late, QH) == datetime(2026, 1, 16, 6, 30, tzinfo=PT)


def test_matches_trigger_id_is_independent_of_winning_trigger(config):
    text = "Trump said tariffs on China would increase"
    trigger = match_trigger(text, config.alert_triggers)
    assert trigger is not None
    assert matches_trigger_id(text, config.alert_triggers, "verified_official_statement") is True
    assert matches_trigger_id(text, config.alert_triggers, "export_control_or_sanction") is True
    assert matches_trigger_id(text, config.alert_triggers, "military_or_geopolitical_escalation") is False


def test_release_held_alerts_consolidates_and_counts_as_one_message(config, conn):
    quiet_now = datetime(2026, 1, 15, 1, 0, tzinfo=PT)
    _seed_event(
        conn, event_id="E-HELD-1", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_held_1", 0, "bis.gov")], now=quiet_now,
    )
    _seed_event(
        conn, event_id="E-HELD-2", title="Commerce export control rule on equipment",
        excerpt="Commerce announced an export control rule on semiconductor equipment.",
        sources=[("bis_held_2", 0, "commerce.gov")], now=quiet_now,
    )
    row1 = conn.execute("SELECT * FROM events WHERE id='E-HELD-1'").fetchone()
    row2 = conn.execute("SELECT * FROM events WHERE id='E-HELD-2'").fetchone()

    r1 = process_event_for_alerts(conn, config, row1, quiet_now)
    r2 = process_event_for_alerts(conn, config, row2, quiet_now)
    assert r1.action == "held_quiet_hours"
    assert r2.action == "held_quiet_hours"

    release_time = datetime(2026, 1, 15, 6, 45, tzinfo=PT)
    result = release_held_alerts(config, conn, now=release_time)

    assert set(result.released_event_ids) == {"E-HELD-1", "E-HELD-2"}
    assert "Overnight Important Update" in result.rendered_text

    rows = conn.execute(
        "SELECT DISTINCT message_hash FROM alert_queue WHERE event_id IN ('E-HELD-1','E-HELD-2') AND state='sent'"
    ).fetchall()
    assert len(rows) == 1  # both share one message_hash

    budget_count = conn.execute(
        "SELECT COUNT(DISTINCT message_hash) AS n FROM alert_queue WHERE state='sent' AND severity='S2'"
    ).fetchone()["n"]
    assert budget_count == 1  # counts as one message toward the daily S2 budget


def test_cmd_why_and_cmd_sources(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-CMD-1", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_cmd_1", 0, "bis.gov")], now=now,
    )
    why = commands.cmd_why(conn, "E-CMD-1")
    assert "BIS export control rule on chips" in why
    assert "official" in why

    sources = commands.cmd_sources(conn, "E-CMD-1")
    assert "bis_cmd_1" in sources or "T0" in sources


def test_cmd_mute_suppresses_s2_but_not_s3(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    reply = commands.cmd_mute(conn, "2h", now=now)
    assert "Muted S2 alerts" in reply

    _seed_event(
        conn, event_id="E-MUTE-S2", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_mute", 0, "bis.gov")], now=now,
    )
    _seed_event(
        conn, event_id="E-MUTE-S3", title="Systemic financial instability triggers market shutdown",
        excerpt="Regulators reported systemic financial instability leading to a broad market shutdown.",
        sources=[("fed_mute", 0, "federalreserve.gov")], now=now,
    )
    row_s2 = conn.execute("SELECT * FROM events WHERE id='E-MUTE-S2'").fetchone()
    row_s3 = conn.execute("SELECT * FROM events WHERE id='E-MUTE-S3'").fetchone()

    result_s2 = process_event_for_alerts(conn, config, row_s2, now)
    result_s3 = process_event_for_alerts(conn, config, row_s3, now)

    assert result_s2.action == "suppressed_muted"
    assert result_s3.action == "sent"


def test_cmd_topic_and_cmd_now(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-TOPIC-1", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_topic", 0, "bis.gov")], now=now,
    )
    conn.execute("UPDATE events SET category='AI & Semiconductors' WHERE id='E-TOPIC-1'")

    tech_reply = commands.cmd_topic(conn, config, "tech", now=now)
    assert "E-TOPIC-1" in tech_reply

    now_reply = commands.cmd_now(conn, config, now=now)
    assert "E-TOPIC-1" in now_reply

    unknown_reply = commands.cmd_topic(conn, config, "bogus", now=now)
    assert "Unknown topic" in unknown_reply
