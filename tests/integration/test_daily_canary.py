"""Bounded three-delivery daily canary: fail-closed pre-send checks (chat
allowlist, canary_index bounds, excluded category, unmapped source URL,
oversized message), honest "no qualifying developments" message, canary
labeling, and durable no-retry-on-failure idempotency via
delivery.telegram.mark_attempting."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from news_intel import db
from news_intel.jobs.daily_canary import run_daily_canary
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT, insert_source_row

_NOW = datetime(2026, 8, 6, 5, 0, tzinfo=UTC)  # 2026-08-05 22:00 PDT


@pytest.fixture
def canary_config(tmp_path: Path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    yield cfg
    reset_config_cache()


@pytest.fixture
def canary_config_with_token(tmp_path: Path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-not-used-for-real")
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    yield cfg
    reset_config_cache()


@pytest.fixture
def canary_conn_with_token(canary_config_with_token):
    connection = db.connect(canary_config_with_token.data_dir / "news-intel.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def canary_conn(canary_config):
    connection = db.connect(canary_config.data_dir / "news-intel.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


def _seed_one_event(config, conn, now, *, event_id="E-canary-0001", category_override=None, url="https://x.test/0"):
    source_id = "canary_source"
    insert_source_row(conn, source_id, 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, 'unverified')",
        (event_id, "NVIDIA export control chip rule", now.isoformat(), now.isoformat(), now.isoformat()),
    )
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (source_id, "ext0", url, "hash0", "NVIDIA export control chip rule",
         "nvidia export control chip rule", "New export control rule affects AI chip shipments.",
         now.isoformat(), "content0", "bis.gov"),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
        "VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )
    update_confirmation_state(conn, event_id)
    scores = score_event(conn, event_id, config, is_new_event=True, now=now)
    weights = config.policy.scoring.weights.model_dump()
    persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)
    if category_override is not None:
        conn.execute("UPDATE events SET category = ? WHERE id = ?", (category_override, event_id))
    return event_id


def test_no_qualifying_events_sends_honest_message_not_padded(canary_config, canary_conn):
    summary = run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)

    # No TELEGRAM_BOT_TOKEN is configured in this test env, so the send is a
    # (correctly) recorded dry-run rather than a real Telegram call -- see
    # TelegramSender.send()'s `dry_run or not self.bot_token` branch.
    # `sent` reflects a *real* send and is asserted separately once a fake
    # bot token + mocked Bot is wired up; here we assert the attempt itself
    # was made and not fail-closed.
    assert summary.fail_closed is False
    assert summary.ran_send_attempt is True
    assert summary.event_count == 0
    assert "[DAILY CANARY 1/3]" in summary.rendered_text
    assert "No qualifying developments were found" in summary.rendered_text


def test_qualifying_event_is_sent_with_canary_label(canary_config, canary_conn):
    _seed_one_event(canary_config, canary_conn, _NOW)

    summary = run_daily_canary(canary_config, canary_conn, canary_index=2, now=_NOW)

    assert summary.fail_closed is False
    assert summary.ran_send_attempt is True
    assert summary.event_count == 1
    assert summary.rendered_text.startswith("[DAILY CANARY 2/3]")
    assert "NVIDIA export control chip rule" in summary.rendered_text


def test_canary_index_out_of_bounds_fails_closed_without_send(canary_config, canary_conn):
    summary = run_daily_canary(canary_config, canary_conn, canary_index=4, now=_NOW)

    assert summary.fail_closed is True
    assert summary.ran_send_attempt is False
    assert "outside the bounded" in summary.fail_reason
    count = canary_conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
    assert count == 0


def test_no_allowlisted_chat_fails_closed(tmp_path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    connection = db.connect(cfg.data_dir / "news-intel.db")
    db.run_migrations(connection)
    try:
        summary = run_daily_canary(cfg, connection, canary_index=1, now=_NOW)
    finally:
        connection.close()
        reset_config_cache()

    assert summary.fail_closed is True
    assert "exactly one allowlisted" in summary.fail_reason


def test_multiple_allowlisted_chats_fails_closed(tmp_path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555,556")
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    connection = db.connect(cfg.data_dir / "news-intel.db")
    db.run_migrations(connection)
    try:
        summary = run_daily_canary(cfg, connection, canary_index=1, now=_NOW)
    finally:
        connection.close()
        reset_config_cache()

    assert summary.fail_closed is True
    assert "found 2" in summary.fail_reason


def test_excluded_category_fails_closed_even_if_score_qualified(canary_config, canary_conn):
    """Defense in depth: event_repo.qualified_events_since only filters by
    score/eligibility-time, never by category (noise/Unclassified are real
    stored category values) -- the canary must independently refuse to send
    one even if some future scoring change let it slip through on score
    alone."""
    _seed_one_event(canary_config, canary_conn, _NOW, category_override="noise")

    summary = run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)

    assert summary.fail_closed is True
    assert "excluded category" in summary.fail_reason
    count = canary_conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
    assert count == 0


def test_event_without_mapped_source_url_fails_closed(canary_config, canary_conn):
    # canonical_url is NOT NULL at the schema level, so an empty string
    # (falsy, same as the code's `any(s.get("url") for s in ...)` check
    # would treat a missing URL) is the closest reachable proxy for "no
    # mapped source URL" without violating the raw_items constraint.
    _seed_one_event(canary_config, canary_conn, _NOW, url="")

    summary = run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)

    assert summary.fail_closed is True
    assert "no mapped source URL" in summary.fail_reason


def test_oversized_message_fails_closed_without_truncating(canary_config, canary_conn):
    _seed_one_event(canary_config, canary_conn, _NOW)
    canary_config.policy.digests.telegram_hard_limit_chars = 10

    summary = run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)

    assert summary.fail_closed is True
    assert "exceeds the 10-char" in summary.fail_reason
    count = canary_conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
    assert count == 0


def test_second_call_for_same_pacific_date_is_not_retried(canary_config, canary_conn):
    _seed_one_event(canary_config, canary_conn, _NOW)

    first = run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)
    assert first.fail_closed is False
    assert first.ran_send_attempt is True

    second = run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)
    assert second.fail_closed is True
    assert "already been attempted" in second.fail_reason

    count = canary_conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE idempotency_key = ?", (first.idempotency_key,)
    ).fetchone()["n"]
    assert count == 1


def test_failed_send_pre_mark_blocks_retry_and_surfaces_the_error(canary_config, canary_conn, monkeypatch):
    _seed_one_event(canary_config, canary_conn, _NOW)

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated network failure")

    import news_intel.jobs.daily_canary as canary_module

    monkeypatch.setattr(canary_module.TelegramSender, "send", _boom)

    with pytest.raises(RuntimeError, match="simulated network failure"):
        run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)

    row = canary_conn.execute("SELECT * FROM deliveries").fetchone()
    assert row is not None
    assert row["status"] == "attempting"

    # A second attempt for the same Pacific date must not retry, even though
    # the first attempt never reached "sent" -- mark_attempting() durably
    # pre-marks the date as attempted before any network call.
    monkeypatch.undo()
    second = run_daily_canary(canary_config, canary_conn, canary_index=1, now=_NOW)
    assert second.fail_closed is True
    assert "already been attempted" in second.fail_reason

    count = canary_conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
    assert count == 1


def test_real_send_with_bot_token_reports_sent_true_and_message_id(
    canary_config_with_token, canary_conn_with_token
):
    _seed_one_event(canary_config_with_token, canary_conn_with_token, _NOW)
    fake_message = SimpleNamespace(message_id=9001)

    with patch("telegram.Bot.send_message", new=AsyncMock(return_value=fake_message)):
        summary = run_daily_canary(canary_config_with_token, canary_conn_with_token, canary_index=3, now=_NOW)

    assert summary.fail_closed is False
    assert summary.sent is True
    assert summary.message_ids == ["9001"]
    assert summary.rendered_text.startswith("[DAILY CANARY 3/3]")

    row = canary_conn_with_token.execute("SELECT * FROM deliveries").fetchone()
    assert row["status"] == "sent"
    assert row["telegram_message_id"] == "9001"
