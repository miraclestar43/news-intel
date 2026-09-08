"""2026-09-07: tests for the missing-delivery observability added after two
consecutive incidents were noticed only because the user saw Telegram go
quiet. The critical case is `never_ran` -- a run that left no trace while
systemd reported success -- which nothing in the system previously
reported."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from news_intel import db
from news_intel.jobs.daily_health import edition_day_health

PT_DATE = date(2026, 9, 6)
DAY_START_UTC = datetime(2026, 9, 6, 7, 0, tzinfo=UTC)  # ~00:00 PDT


@pytest.fixture
def hconn(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    db.run_migrations(conn)
    yield conn
    conn.close()


def _healthy_collector(conn, n=288, per=600):
    for i in range(n):
        conn.execute(
            "INSERT INTO job_runs (job_name, started_at, status, detail_json) VALUES (?,?,?,?)",
            ("collect", (DAY_START_UTC + timedelta(minutes=i)).isoformat(), "success",
             json.dumps({"fetched_items": per})),
        )


def _digest(conn, *, edition, text, events, created=None):
    cur = conn.execute(
        "INSERT INTO digests (kind, window_start, window_end, generation_state, rendered_text, "
        "model_used, fallback_used, created_at, edition_id) "
        "VALUES ('daily',?,?,'generated',?,'m',1,?,?)",
        (DAY_START_UTC.isoformat(), DAY_START_UTC.isoformat(), text,
         (created or DAY_START_UTC + timedelta(hours=5)).isoformat(), edition),
    )
    did = cur.lastrowid
    for i in range(events):
        event_id = f"E-{edition}-{i}"
        conn.execute(
            "INSERT OR IGNORE INTO events (id, primary_title, first_seen_at, last_updated_at, "
            "published_anchor_at, confirmation_state) VALUES (?,?,?,?,?,'official')",
            (event_id, f"Story {i}", DAY_START_UTC.isoformat(), DAY_START_UTC.isoformat(),
             DAY_START_UTC.isoformat()),
        )
        conn.execute(
            "INSERT INTO digest_events (digest_id, event_id, rank, section) VALUES (?,?,?,'top3')",
            (did, event_id, i + 1),
        )
    return did


def _delivery(conn, key="daily:2026-09-06:x"):
    conn.execute(
        "INSERT INTO deliveries (idempotency_key, chat_id_hash, kind, status, created_at, updated_at) "
        "VALUES (?,?,?,'sent',?,?)",
        (key, "h", "daily", (DAY_START_UTC + timedelta(hours=5)).isoformat(),
         (DAY_START_UTC + timedelta(hours=5)).isoformat()),
    )


def _h(conn, edition="en_global"):
    return edition_day_health(conn, edition_id=edition, pt_date=PT_DATE)


def test_never_ran_is_the_signature_of_both_incidents(hconn):
    """No digest row at all while the collector was healthy -- exactly what
    Sep 6/7 looked like, and what systemd called 'success'."""
    _healthy_collector(hconn)
    result = _h(hconn)
    assert result.status == "never_ran"
    assert result.ok is False
    assert "collector was healthy" in result.detail


def test_degraded_collection_is_not_reported_as_never_ran(hconn):
    """A starved window is a different problem and must not be blamed on
    the digest job."""
    _healthy_collector(hconn, n=3, per=5)
    result = _h(hconn)
    assert result.status == "degraded_collection"
    assert result.ok is False


def test_delivered_single_message(hconn):
    _healthy_collector(hconn)
    _digest(hconn, edition="en_global", text="A brief.\n\nBody.", events=3)
    _delivery(hconn)
    result = _h(hconn)
    assert result.status == "delivered"
    assert result.ok is True
    assert result.parts == 1
    assert result.delivered is True


def test_delivered_multipart_is_distinguished(hconn):
    _healthy_collector(hconn)
    _digest(hconn, edition="en_global", text="\n\n".join("B" * 2000 for _ in range(4)), events=3)
    _delivery(hconn)
    result = _h(hconn)
    assert result.status == "delivered_multipart"
    assert result.ok is True
    assert result.parts > 1


def test_zero_qualified_is_ok_not_a_failure(hconn):
    """A genuinely empty day is healthy -- the system looked and found
    nothing. It must NOT be conflated with never_ran."""
    _healthy_collector(hconn)
    _digest(hconn, edition="en_global", text="No qualifying developments were found.", events=0)
    _delivery(hconn)
    result = _h(hconn)
    assert result.status == "zero_qualified"
    assert result.ok is True


def test_delivery_failed_when_rendered_but_not_sent(hconn):
    _healthy_collector(hconn)
    _digest(hconn, edition="en_global", text="A real brief.\n\nWith content.", events=3)
    # no delivery row
    result = _h(hconn)
    assert result.status == "delivery_failed"
    assert result.ok is False


def test_editions_are_reported_independently(hconn):
    """One edition delivering must never mask the other going silent --
    both editions failed together on Sep 6/7, but the reverse must also be
    detectable."""
    _healthy_collector(hconn)
    _digest(hconn, edition="en_global", text="EN brief.\n\nBody.", events=2)
    _delivery(hconn)
    assert _h(hconn, "en_global").status == "delivered"
    assert _h(hconn, "zh_cn").status == "never_ran"
