"""2026-08-23 repair: the production-send near-miss and its fix.

Real incident: calling run_weekly_production() directly (to verify its
Sunday-window gate, during an incident-response replay) was one config-with-
real-credentials away from a genuine Telegram send -- it returned safely
that time ONLY because the isolated worktree had no .env file, so
config.secrets.allowed_chat_id_list resolved empty and the pre-existing
"len(allowed) != 1" fail-closed check caught it. Missing credentials is not
an acceptable safety mechanism.

This file proves the actual fix: with a config carrying REAL-LOOKING,
PRESENT credentials (a bot token and exactly one allowed chat id -- the
precise scenario that used to reach TelegramSender), all three production
entrypoints (run_daily_production/run_weekly_production/
run_monthly_production) still make ZERO calls into TelegramSender.send
unless the caller passes the explicit `authorization` capability -- and
that WITH authorization, the exact same config does reach the send call,
proving the mechanism enables real production use rather than just always
blocking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from news_intel import db
from news_intel.delivery.telegram import TelegramSender
from news_intel.jobs.daily_production import run_daily_production
from news_intel.jobs.delivery_authorization import (
    PRODUCTION_DELIVERY_AUTHORIZED,
    ProductionDeliveryAuthorization,
)
from news_intel.jobs.monthly_production import run_monthly_production
from news_intel.jobs.weekly_production import run_weekly_production
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT, insert_source_row
from tests.integration.test_monthly_job import FIRST_MONDAY_PT, _seed_full_month
from tests.integration.test_weekly_job import SUNDAY_PT, _seed_full_week

_DAILY_NOW = datetime(2026, 8, 10, 22, 0, tzinfo=UTC)  # a Monday, not the weekly/monthly slot


@pytest.fixture
def real_looking_config(tmp_path, monkeypatch):
    """The exact 'developer worktree with real environment variables'
    scenario: a bot token and exactly one allowed chat id are PRESENT (not
    deleted), matching what a genuinely misconfigured or leaked-.env
    replay environment would look like -- the near-miss this file exists
    to close off."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-but-present-token-shaped-string")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    assert cfg.secrets.telegram_bot_token  # sanity: credentials really are present
    assert cfg.secrets.allowed_chat_id_list == [555]
    yield cfg
    reset_config_cache()


@pytest.fixture
def conn(real_looking_config):
    connection = db.connect(real_looking_config.data_dir / "news-intel.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def send_spy(monkeypatch):
    """A fake sender: replaces TelegramSender.send with a spy that records
    every call and never touches the network. Patching the class attribute
    (not a per-module import binding) affects every job module's reference
    to the same class object."""
    calls: list[dict] = []

    async def _fake_send(self, conn, *, chat_id, text, kind, idempotency_key, max_chars, dry_run=True,
                          parse_mode=None):
        calls.append({"chat_id": chat_id, "kind": kind, "idempotency_key": idempotency_key, "dry_run": dry_run})
        from news_intel.delivery.telegram import SendResult
        return SendResult(idempotency_key=idempotency_key, already_sent=False, message_ids=["fake-1"], dry_run=True)

    monkeypatch.setattr(TelegramSender, "send", _fake_send)
    return calls


def _seed_daily_event(conn, config, *, event_id="E-1", now=_DAILY_NOW):
    source_id = f"src_{event_id}"
    insert_source_row(conn, source_id, 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
        "confirmation_state) VALUES (?, ?, ?, ?, ?, 'official')",
        (event_id, "A qualifying story for the safety-contract test", now.isoformat(), now.isoformat(),
         now.isoformat()),
    )
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (source_id, f"{event_id}-0", "https://x.test/0", f"{event_id}-hash",
         "A qualifying story for the safety-contract test", "a qualifying story", "A complete sentence here.",
         now.isoformat(), f"{event_id}-content", source_id),
    )
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, cur.lastrowid),
    )
    update_confirmation_state(conn, event_id, now=now)
    scores = score_event(conn, event_id, config, is_new_event=True, now=now)
    weights = config.policy.scoring.weights.model_dump()
    persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)
    conn.execute(
        "UPDATE events SET category = 'AI & Semiconductors', final_score = 90, significance = 90, "
        "market_impact = 90 WHERE id = ?",
        (event_id,),
    )


# --- default (no authorization): zero network calls, even with real creds --


def test_daily_production_without_authorization_never_calls_send(real_looking_config, conn, send_spy):
    _seed_daily_event(conn, real_looking_config)
    summary = run_daily_production(
        real_looking_config, conn, start_date=_DAILY_NOW.date() - timedelta(days=1), now=_DAILY_NOW,
    )
    assert summary.top3_count >= 1  # the digest was genuinely constructed, not just skipped
    assert summary.sent is False
    assert summary.sent_dry_run is True
    assert send_spy == []  # zero calls into TelegramSender.send


def test_weekly_production_without_authorization_never_calls_send(real_looking_config, conn, send_spy):
    _seed_full_week(conn)
    summary = run_weekly_production(real_looking_config, conn, now=SUNDAY_PT.astimezone(UTC))
    assert summary.skipped_late_catchup is False
    assert summary.trend_count >= 1
    assert summary.sent is False
    assert send_spy == []


def test_monthly_production_without_authorization_never_calls_send(real_looking_config, conn, send_spy):
    _seed_full_month(conn)
    summary = run_monthly_production(real_looking_config, conn, now=FIRST_MONDAY_PT.astimezone(UTC))
    assert summary.skipped_late_catchup is False
    assert summary.trend_count >= 1
    assert summary.sent is False
    assert send_spy == []


# --- with explicit authorization: the same real-looking config DOES reach --
# --- the send call, proving this is a real capability, not a dead switch --


def test_daily_production_with_authorization_reaches_send(real_looking_config, conn, send_spy):
    _seed_daily_event(conn, real_looking_config)
    run_daily_production(
        real_looking_config, conn, start_date=_DAILY_NOW.date() - timedelta(days=1), now=_DAILY_NOW,
        authorization=PRODUCTION_DELIVERY_AUTHORIZED,
    )
    assert len(send_spy) >= 1
    assert send_spy[0]["chat_id"] == 555


def test_weekly_production_with_authorization_reaches_send(real_looking_config, conn, send_spy):
    _seed_full_week(conn)
    run_weekly_production(
        real_looking_config, conn, now=SUNDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED,
    )
    assert len(send_spy) >= 1
    assert send_spy[0]["chat_id"] == 555


def test_monthly_production_with_authorization_reaches_send(real_looking_config, conn, send_spy):
    _seed_full_month(conn)
    run_monthly_production(
        real_looking_config, conn, now=FIRST_MONDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED,
    )
    assert len(send_spy) >= 1
    assert send_spy[0]["chat_id"] == 555


# --- the authorization object itself carries no bypassable state -----------


def test_authorization_object_is_a_bare_immutable_marker():
    """It must not be constructible with parameters that could smuggle a
    real chat_id/token through it -- it is a pure capability marker."""
    with pytest.raises(TypeError):
        ProductionDeliveryAuthorization(chat_id=555)  # type: ignore[call-arg]
