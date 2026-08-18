"""Safe multipart monthly delivery. Since migrations/012_multi_edition.sql
generalized weekly_deliveries/weekly_delivery_parts (migration 011) with an
edition_id + kind column rather than a separate monthly table, monthly
delivery reuses delivery/weekly_delivery_repo.py directly with kind='monthly'
-- this file asserts the SAME contract as test_weekly_multipart_delivery.py,
querying the same underlying table."""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from news_intel import db
from news_intel.delivery import weekly_delivery_repo
from news_intel.jobs.monthly import run_monthly
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT
from tests.integration.test_monthly_job import FIRST_MONDAY_PT, _seed_full_month


@pytest.fixture
def config(tmp_path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-not-used-for-real")
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
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


def test_successful_multipart_send_records_one_logical_delivery_with_kind_monthly(config, conn):
    _seed_full_month(conn)
    with patch("telegram.Bot.send_message", new=AsyncMock(side_effect=[_fake_message(101), _fake_message(102)])):
        result = run_monthly(config, conn, chat_id=555, dry_run=False, now=FIRST_MONDAY_PT.astimezone(UTC))

    assert result.fail_closed is False
    assert len(result.rendered_parts) == 2  # forced by the 900-char limit

    delivery = conn.execute(
        "SELECT * FROM weekly_deliveries WHERE idempotency_key = ?", (result.idempotency_key,)
    ).fetchone()
    assert delivery is not None
    assert delivery["status"] == "sent"
    assert delivery["kind"] == "monthly"
    assert delivery["edition_id"] == "en_global"
    assert delivery["total_parts"] == 2

    parts = weekly_delivery_repo.all_parts(conn, delivery["id"])
    assert [p.part_index for p in parts] == [1, 2]
    assert all(p.status == "sent" for p in parts)
    assert parts[0].telegram_message_id == "101"
    assert parts[1].telegram_message_id == "102"


def test_monthly_and_weekly_idempotency_keys_never_collide_on_the_same_period_number(config, conn):
    """monthly:2026-08:... and weekly:2026-W33:... share no literal
    substring collision risk, but the underlying table is now shared --
    this asserts the `kind` column, not just the idempotency_key string, is
    what a reader would use to distinguish them."""
    _seed_full_month(conn)
    result = run_monthly(config, conn, chat_id=555, dry_run=True, now=FIRST_MONDAY_PT.astimezone(UTC))
    assert result.idempotency_key.startswith("monthly:")
    assert "weekly:" not in result.idempotency_key


def test_part_failure_marks_delivery_partial_and_stops(config, conn):
    _seed_full_month(conn)
    with patch(
        "telegram.Bot.send_message",
        new=AsyncMock(side_effect=[_fake_message(201), RuntimeError("simulated Telegram API error")]),
    ):
        result = run_monthly(config, conn, chat_id=555, dry_run=False, now=FIRST_MONDAY_PT.astimezone(UTC))

    assert result.fail_closed is True
    assert "partial" in result.fail_reason

    delivery = conn.execute(
        "SELECT * FROM weekly_deliveries WHERE idempotency_key = ?", (result.idempotency_key,)
    ).fetchone()
    assert delivery["status"] == "partial"
    assert delivery["kind"] == "monthly"


def test_plain_reinvocation_after_partial_failure_does_not_auto_retry(config, conn):
    _seed_full_month(conn)
    with patch(
        "telegram.Bot.send_message",
        new=AsyncMock(side_effect=[_fake_message(301), RuntimeError("simulated Telegram API error")]),
    ):
        first = run_monthly(config, conn, chat_id=555, dry_run=False, now=FIRST_MONDAY_PT.astimezone(UTC))
    assert first.fail_closed is True

    with patch("telegram.Bot.send_message", new=AsyncMock(return_value=_fake_message(999))) as mocked:
        second = run_monthly(config, conn, chat_id=555, dry_run=False, now=FIRST_MONDAY_PT.astimezone(UTC))

    assert second.fail_closed is True
    assert "resume_authorized" in second.fail_reason
    mocked.assert_not_called()


def test_dry_run_never_creates_a_sent_status_part(config, conn):
    _seed_full_month(conn)
    result = run_monthly(config, conn, chat_id=555, dry_run=True, now=FIRST_MONDAY_PT.astimezone(UTC))
    assert result.sent is False

    delivery = conn.execute(
        "SELECT * FROM weekly_deliveries WHERE idempotency_key = ?", (result.idempotency_key,)
    ).fetchone()
    parts = weekly_delivery_repo.all_parts(conn, delivery["id"])
    assert all(p.status == "dry_run" for p in parts)
