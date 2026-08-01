"""Deployment Gate A item 3: dry-run/shadow execution must never be able to
mark a message as delivered in the production ledger, consume the
production S2 daily budget, suppress a later real delivery via a stale
idempotency record, or mutate production mute/feedback state.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState
from news_intel.delivery.telegram import TelegramSender
from news_intel.jobs import collect as collect_module
from news_intel.jobs.collect import run_collect
from news_intel.settings import SourceEntry


def test_dry_run_record_never_suppresses_a_later_real_send(conn):
    sender = TelegramSender(bot_token="fake-token-not-used-for-real", allowed_chat_ids=[555])
    key = "daily:2026-08-01:shadowtest"

    async def _run():
        # A shadow/preview pass, twice -- must stay idempotent among itself.
        r1 = await sender.send(
            conn, chat_id=555, text="preview text", kind="daily", idempotency_key=key,
            max_chars=4000, dry_run=True,
        )
        r2 = await sender.send(
            conn, chat_id=555, text="preview text", kind="daily", idempotency_key=key,
            max_chars=4000, dry_run=True,
        )
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1.already_sent is False
    assert r2.already_sent is True  # repeated dry-run is idempotent with itself

    row = conn.execute("SELECT status FROM deliveries WHERE idempotency_key = ?", (key,)).fetchone()
    assert row["status"] == "dry_run"  # never marked as a real "sent" delivery

    # Now credentials are configured and the operator runs for real, same key.
    fake_message = SimpleNamespace(message_id=4242)
    with patch("telegram.Bot.send_message", new=AsyncMock(return_value=fake_message)):
        real_result = asyncio.run(
            sender.send(
                conn, chat_id=555, text="preview text", kind="daily", idempotency_key=key,
                max_chars=4000, dry_run=False,
            )
        )

    assert real_result.already_sent is False  # the prior dry_run record did NOT suppress this
    assert real_result.dry_run is False
    assert real_result.message_ids == ["4242"]

    row = conn.execute("SELECT status FROM deliveries WHERE idempotency_key = ?", (key,)).fetchone()
    assert row["status"] == "sent"  # now genuinely marked delivered

    # A further real attempt for the same key is correctly suppressed (true idempotency).
    with patch("telegram.Bot.send_message", new=AsyncMock(return_value=fake_message)) as mocked_send:
        repeat_result = asyncio.run(
            sender.send(
                conn, chat_id=555, text="preview text", kind="daily", idempotency_key=key,
                max_chars=4000, dry_run=False,
            )
        )
    assert repeat_result.already_sent is True
    mocked_send.assert_not_called()


def test_real_send_marker_is_never_overwritten_by_a_later_dry_run_probe(conn):
    """A later --dry-run preview, after a real send already happened for the
    same key, must recognize the send is final and not touch/duplicate the
    ledger row (defense in depth: shadow probes must never look like they
    re-delivered something already sent)."""
    sender = TelegramSender(bot_token="fake-token", allowed_chat_ids=[555])
    key = "daily:2026-08-02:shadowtest2"
    fake_message = SimpleNamespace(message_id=99)

    with patch("telegram.Bot.send_message", new=AsyncMock(return_value=fake_message)):
        real_result = asyncio.run(
            sender.send(
                conn, chat_id=555, text="real text", kind="daily", idempotency_key=key,
                max_chars=4000, dry_run=False,
            )
        )
    assert real_result.already_sent is False

    dry_probe = asyncio.run(
        sender.send(
            conn, chat_id=555, text="real text", kind="daily", idempotency_key=key,
            max_chars=4000, dry_run=True,
        )
    )
    assert dry_probe.already_sent is True

    row = conn.execute("SELECT status, attempts FROM deliveries WHERE idempotency_key = ?", (key,)).fetchone()
    assert row["status"] == "sent"
    assert row["attempts"] == 1  # the dry-run probe did not touch/re-attempt the real record


class _FakeCollectAdapter:
    def fetch(self, ctx, cursor):
        return FetchOutcome(
            [
                CollectedItem(
                    source_id=ctx.id, external_id="shadow-1", canonical_url="https://shadow.test/story",
                    title="Shadow pilot collected story", summary_excerpt="A collected story for the shadow pilot.",
                    language="en", published_at=None, origin_family="shadow.test",
                )
            ],
            "cursor-1", SourceHealthState.HEALTHY,
        )


def test_collection_only_run_never_touches_alert_delivery_mute_or_feedback_tables(config, conn, monkeypatch):
    """The actual shadow pilot installs only pi-news-collect.timer. Proves
    run_collect() has no code path that writes to alert_queue, deliveries,
    runtime_state, or feedback -- those tables must be completely untouched
    by a real (non-dry-run) collection pass."""
    config.sources.sources.clear()
    config.sources.sources.append(
        SourceEntry(id="shadow_source", name="Shadow", adapter="rss", tier=0, enabled=True, url="https://x/1")
    )
    monkeypatch.setattr(collect_module, "get_adapter", lambda adapter_type: _FakeCollectAdapter())

    summary = run_collect(config, conn, now=datetime(2026, 7, 31, 15, 0, tzinfo=UTC))

    assert summary.new_raw_items == 1
    assert summary.new_events == 1

    for table in ("alert_queue", "deliveries", "runtime_state", "feedback"):
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert count == 0, f"run_collect wrote to {table}, which the shadow pilot must never touch"
