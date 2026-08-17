"""2026-08-17: safe multipart weekly delivery -- explicit weekly_deliveries/
weekly_delivery_parts tracking (migrations/011_weekly_multipart_delivery.sql,
delivery/weekly_delivery_repo.py), wired into jobs/weekly.py's send loop."""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from news_intel import db
from news_intel.delivery import weekly_delivery_repo
from news_intel.jobs.weekly import run_weekly
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT
from tests.integration.test_weekly_job import SUNDAY_PT, _seed_full_week


@pytest.fixture
def config(tmp_path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-not-used-for-real")
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    # Force a real two-part split from a modest amount of seeded content,
    # without needing to construct an enormous fixture.
    cfg.policy.digests.telegram_hard_limit_chars = 900
    yield cfg
    reset_config_cache()


@pytest.fixture
def conn(config):
    connection = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


def _fake_message(message_id: int):
    return SimpleNamespace(message_id=message_id)


def test_successful_multipart_send_records_one_logical_delivery_and_ordered_parts(config, conn):
    _seed_full_week(conn)
    with patch("telegram.Bot.send_message", new=AsyncMock(side_effect=[_fake_message(101), _fake_message(102)])):
        result = run_weekly(config, conn, chat_id=555, dry_run=False, now=SUNDAY_PT.astimezone(UTC))

    assert result.fail_closed is False
    assert len(result.rendered_parts) == 2  # forced by the 900-char limit

    delivery = conn.execute(
        "SELECT * FROM weekly_deliveries WHERE idempotency_key = ?", (result.idempotency_key,)
    ).fetchone()
    assert delivery is not None
    assert delivery["status"] == "sent"
    assert delivery["total_parts"] == 2

    parts = weekly_delivery_repo.all_parts(conn, delivery["id"])
    assert [p.part_index for p in parts] == [1, 2]
    assert all(p.status == "sent" for p in parts)
    assert parts[0].telegram_message_id == "101"
    assert parts[1].telegram_message_id == "102"
    # Per-part text hash actually corresponds to the rendered part text.
    assert parts[0].text_hash == weekly_delivery_repo.text_hash(result.rendered_parts[0])
    assert parts[1].text_hash == weekly_delivery_repo.text_hash(result.rendered_parts[1])


def test_part_two_failure_marks_delivery_partial_and_stops(config, conn):
    _seed_full_week(conn)
    with patch(
        "telegram.Bot.send_message",
        new=AsyncMock(side_effect=[_fake_message(201), RuntimeError("simulated Telegram API error")]),
    ):
        result = run_weekly(config, conn, chat_id=555, dry_run=False, now=SUNDAY_PT.astimezone(UTC))

    assert result.fail_closed is True
    assert "partial" in result.fail_reason

    delivery = conn.execute(
        "SELECT * FROM weekly_deliveries WHERE idempotency_key = ?", (result.idempotency_key,)
    ).fetchone()
    assert delivery["status"] == "partial"

    parts = weekly_delivery_repo.all_parts(conn, delivery["id"])
    assert parts[0].status == "sent"
    assert parts[0].telegram_message_id == "201"
    assert parts[1].status == "failed"


def test_plain_reinvocation_after_partial_failure_does_not_auto_retry(config, conn):
    _seed_full_week(conn)
    with patch(
        "telegram.Bot.send_message",
        new=AsyncMock(side_effect=[_fake_message(301), RuntimeError("simulated Telegram API error")]),
    ):
        first = run_weekly(config, conn, chat_id=555, dry_run=False, now=SUNDAY_PT.astimezone(UTC))
    assert first.fail_closed is True

    with patch("telegram.Bot.send_message", new=AsyncMock(return_value=_fake_message(999))) as mocked:
        second = run_weekly(config, conn, chat_id=555, dry_run=False, now=(SUNDAY_PT).astimezone(UTC))

    assert second.fail_closed is True
    assert "resume_authorized" in second.fail_reason
    mocked.assert_not_called()  # no automatic retry -- zero new Telegram contact


def test_explicit_resume_authorized_completes_only_the_failed_part(config, conn):
    _seed_full_week(conn)
    with patch(
        "telegram.Bot.send_message",
        new=AsyncMock(side_effect=[_fake_message(401), RuntimeError("simulated Telegram API error")]),
    ):
        first = run_weekly(config, conn, chat_id=555, dry_run=False, now=SUNDAY_PT.astimezone(UTC))
    assert first.fail_closed is True

    with patch("telegram.Bot.send_message", new=AsyncMock(return_value=_fake_message(402))) as mocked:
        second = run_weekly(
            config, conn, chat_id=555, dry_run=False, now=SUNDAY_PT.astimezone(UTC), resume_authorized=True
        )

    assert second.fail_closed is False
    # Only ONE new call -- part 1 (already sent) is never resent.
    assert mocked.call_count == 1

    delivery = conn.execute(
        "SELECT * FROM weekly_deliveries WHERE idempotency_key = ?", (second.idempotency_key,)
    ).fetchone()
    assert delivery["status"] == "sent"
    parts = weekly_delivery_repo.all_parts(conn, delivery["id"])
    assert parts[0].telegram_message_id == "401"  # untouched from the first attempt
    assert parts[1].telegram_message_id == "402"


def test_dry_run_never_creates_a_sent_status_part(config, conn):
    _seed_full_week(conn)
    result = run_weekly(config, conn, chat_id=555, dry_run=True, now=SUNDAY_PT.astimezone(UTC))
    assert result.sent is False

    delivery = conn.execute(
        "SELECT * FROM weekly_deliveries WHERE idempotency_key = ?", (result.idempotency_key,)
    ).fetchone()
    parts = weekly_delivery_repo.all_parts(conn, delivery["id"])
    assert all(p.status == "dry_run" for p in parts)
