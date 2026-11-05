"""2026-11-04: deliveries.edition_id and edition-scoped daily health.

Production evidence (read-only audit): every daily row -- including the
zh_cn ones, whose keys are `daily:zh_cn:<date>:<chat>` -- was labeled
edition_id='en_global', because the INSERTs omitted the column and got the
migration-012 DEFAULT. daily_health then counted ANY sent row in the day
window as "delivered" for each edition, so one edition's send could hide the
other's missing delivery. The send keys were always correct; these tests pin
them unchanged while fixing the label and the health check."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from news_intel import db
from news_intel.delivery.telegram import TelegramSender, chat_id_hash, mark_attempting
from news_intel.jobs.daily import _daily_idempotency_key, run_daily
from news_intel.jobs.daily_health import _daily_key_prefix, edition_day_health
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT

PT_DATE = date(2026, 9, 6)
DAY_START_UTC = datetime(2026, 9, 6, 7, 0, tzinfo=UTC)  # 00:00 PDT
CHAT = 555


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "e.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


def _row(conn, key):
    return conn.execute("SELECT * FROM deliveries WHERE idempotency_key = ?", (key,)).fetchone()


def _key(edition, day=PT_DATE):
    return _daily_idempotency_key(edition, day.isoformat(), chat_id_hash(CHAT))


# --- write path ---------------------------------------------------------------


def test_mark_attempting_writes_the_explicit_edition(conn):
    key = _key("zh_cn")
    assert mark_attempting(conn, key, CHAT, "daily", edition_id="zh_cn") is True
    assert _row(conn, key)["edition_id"] == "zh_cn"


def test_mark_attempting_without_edition_keeps_the_legacy_default(conn):
    key = _key("en_global")
    mark_attempting(conn, key, CHAT, "daily")
    assert _row(conn, key)["edition_id"] == "en_global"


def test_send_records_edition_and_stays_idempotent(conn):
    sender = TelegramSender(None, [CHAT])
    key = _key("zh_cn")

    def send():
        return asyncio.run(sender.send(conn, chat_id=CHAT, text="简报", kind="daily", idempotency_key=key,
                                       max_chars=4096, dry_run=True, edition_id="zh_cn"))

    first, second = send(), send()
    assert first.already_sent is False and second.already_sent is True
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
    assert _row(conn, key)["edition_id"] == "zh_cn"


def test_status_update_never_relabels_an_existing_row(conn):
    key = _key("zh_cn")
    mark_attempting(conn, key, CHAT, "daily", edition_id="zh_cn")
    # A later sender call (e.g. a caller that forgot the edition) only updates status.
    asyncio.run(TelegramSender(None, [CHAT]).send(conn, chat_id=CHAT, text="x", kind="daily",
                                                  idempotency_key=key, max_chars=4096, dry_run=True))
    assert _row(conn, key)["edition_id"] == "zh_cn"


def test_send_keys_are_unchanged_and_distinct_per_edition(conn):
    h = chat_id_hash(CHAT)
    assert _key("en_global") == f"daily:2026-09-06:{h}"
    assert _key("zh_cn") == f"daily:zh_cn:2026-09-06:{h}"
    mark_attempting(conn, _key("en_global"), CHAT, "daily", edition_id="en_global")
    assert mark_attempting(conn, _key("zh_cn"), CHAT, "daily", edition_id="zh_cn") is True


@pytest.fixture
def config(tmp_path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(CHAT))
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    yield cfg
    reset_config_cache()


@pytest.mark.parametrize("edition", ["en_global", "zh_cn"])
def test_run_daily_dry_run_labels_its_own_edition(config, tmp_path, edition):
    connection = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(connection)
    try:
        now = datetime(2026, 8, 10, 22, 0, tzinfo=UTC)  # a Monday, 15:00 PDT
        summary = run_daily(config, connection, chat_id=CHAT, dry_run=True, now=now, edition_id=edition)
        rows = connection.execute("SELECT idempotency_key, edition_id FROM deliveries").fetchall()
        assert [(r["idempotency_key"], r["edition_id"]) for r in rows] == [(summary.idempotency_key, edition)]
        assert summary.idempotency_key.startswith(_daily_key_prefix(edition, date(2026, 8, 10)))
    finally:
        connection.close()


# --- edition-scoped health ------------------------------------------------------


def _healthy_collector(conn, n=288, per=600):
    for i in range(n):
        conn.execute(
            "INSERT INTO job_runs (job_name, started_at, status, detail_json) VALUES ('collect', ?, 'success', ?)",
            ((DAY_START_UTC + timedelta(minutes=i)).isoformat(), f'{{"fetched_items": {per}}}'),
        )


def _digest(conn, edition, events=2):
    created = (DAY_START_UTC + timedelta(hours=5)).isoformat()
    did = conn.execute(
        "INSERT INTO digests (kind, window_start, window_end, generation_state, rendered_text, model_used, "
        "fallback_used, created_at, edition_id) VALUES ('daily', ?, ?, 'generated', ?, 'm', 1, ?, ?)",
        (created, created, f"{edition} brief.\n\nBody.", created, edition),
    ).lastrowid
    for i in range(events):
        eid = f"E-{edition}-{i}"
        conn.execute(
            "INSERT OR IGNORE INTO events (id, primary_title, first_seen_at, last_updated_at, "
            "published_anchor_at, confirmation_state) VALUES (?, 'S', ?, ?, ?, 'official')",
            (eid, created, created, created),
        )
        conn.execute("INSERT INTO digest_events (digest_id, event_id, rank, section) VALUES (?, ?, ?, 'top3')",
                     (did, eid, i + 1))


def _sent(conn, edition, *, day=PT_DATE, created=None, status="sent", label="en_global"):
    """Insert a delivery row the way production wrote them before the fix:
    the column label defaults to en_global whatever the real edition is."""
    at = (created or DAY_START_UTC + timedelta(hours=6)).isoformat()
    conn.execute(
        "INSERT INTO deliveries (idempotency_key, chat_id_hash, kind, status, created_at, updated_at, edition_id) "
        "VALUES (?, 'h', 'daily', ?, ?, ?, ?)",
        (_key(edition, day), status, at, at, label),
    )


@pytest.fixture
def both_rendered(conn):
    _healthy_collector(conn)
    _digest(conn, "en_global")
    _digest(conn, "zh_cn")
    return conn


def _status(conn, edition, day=PT_DATE):
    return edition_day_health(conn, edition_id=edition, pt_date=day).status


def test_en_only_does_not_mark_zh_delivered(both_rendered):
    _sent(both_rendered, "en_global")
    assert _status(both_rendered, "en_global") == "delivered"
    assert _status(both_rendered, "zh_cn") == "delivery_failed"


def test_zh_only_does_not_mark_en_delivered(both_rendered):
    """The masking case the old window count got wrong."""
    _sent(both_rendered, "zh_cn")
    assert _status(both_rendered, "zh_cn") == "delivered"
    assert _status(both_rendered, "en_global") == "delivery_failed"


def test_both_sent_are_both_delivered(both_rendered):
    _sent(both_rendered, "en_global")
    _sent(both_rendered, "zh_cn")
    assert _status(both_rendered, "en_global") == "delivered"
    assert _status(both_rendered, "zh_cn") == "delivered"


def test_neither_sent_is_delivery_failed_for_both(both_rendered):
    assert _status(both_rendered, "en_global") == "delivery_failed"
    assert _status(both_rendered, "zh_cn") == "delivery_failed"


@pytest.mark.parametrize("label", ["en_global", "zh_cn"])
def test_historical_mislabeled_rows_are_judged_by_key(both_rendered, label):
    # Legacy production rows: a zh_cn send labeled en_global (and, defensively,
    # an en send labeled zh_cn) -- the label must not decide anything.
    _sent(both_rendered, "zh_cn", label=label)
    assert _status(both_rendered, "zh_cn") == "delivered"
    assert _status(both_rendered, "en_global") == "delivery_failed"


def test_separate_days_do_not_count_for_each_other(both_rendered):
    # Next day's sends, even if created inside this day's UTC window, are not this day's delivery.
    _sent(both_rendered, "en_global", day=PT_DATE + timedelta(days=1))
    _sent(both_rendered, "zh_cn", day=PT_DATE + timedelta(days=1))
    assert _status(both_rendered, "en_global") == "delivery_failed"
    assert _status(both_rendered, "zh_cn") == "delivery_failed"


def test_late_evening_send_counts_for_its_own_date(both_rendered):
    # en_global runs ~21:00 PT: its row is created the next UTC day but belongs to PT_DATE.
    _sent(both_rendered, "en_global", created=DAY_START_UTC + timedelta(hours=22))
    assert _status(both_rendered, "en_global") == "delivered"


@pytest.mark.parametrize("status", ["attempting", "dry_run", "failed"])
def test_only_sent_rows_count(both_rendered, status):
    _sent(both_rendered, "zh_cn", status=status)
    assert _status(both_rendered, "zh_cn") == "delivery_failed"


def test_prefix_match_is_exact_not_like(conn):
    # 'zh_cn' contains '_', a LIKE wildcard: a key for a different edition
    # that differs only at that position must not match.
    assert _daily_key_prefix("zh_cn", PT_DATE) == "daily:zh_cn:2026-09-06:"
    assert _daily_key_prefix("en_global", PT_DATE) == "daily:2026-09-06:"
    _healthy_collector(conn)
    _digest(conn, "zh_cn")
    conn.execute(
        "INSERT INTO deliveries (idempotency_key, chat_id_hash, kind, status, created_at, updated_at) "
        "VALUES ('daily:zhXcn:2026-09-06:h', 'h', 'daily', 'sent', ?, ?)",
        (DAY_START_UTC.isoformat(), DAY_START_UTC.isoformat()),
    )
    assert _status(conn, "zh_cn") == "delivery_failed"
