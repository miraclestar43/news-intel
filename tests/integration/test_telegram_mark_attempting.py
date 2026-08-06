"""delivery.telegram.mark_attempting: the durable pre-send idempotency mark
used by the daily canary (jobs/daily_canary.py) so a real send that raises
before TelegramSender.send()'s own _record_delivery is reached still leaves
a row behind, blocking any later retry for the same idempotency key."""

from __future__ import annotations

import asyncio

from news_intel.delivery.telegram import TelegramSender, mark_attempting


def test_mark_attempting_inserts_a_pending_row_and_returns_true(conn):
    ok = mark_attempting(conn, "daily_canary:2026-08-06:abc", 555, "daily_canary")

    assert ok is True
    row = conn.execute(
        "SELECT * FROM deliveries WHERE idempotency_key = ?", ("daily_canary:2026-08-06:abc",)
    ).fetchone()
    assert row["status"] == "attempting"
    assert row["telegram_message_id"] is None


def test_mark_attempting_returns_false_and_does_not_duplicate_if_already_marked(conn):
    first = mark_attempting(conn, "daily_canary:2026-08-06:abc", 555, "daily_canary")
    second = mark_attempting(conn, "daily_canary:2026-08-06:abc", 555, "daily_canary")

    assert first is True
    assert second is False
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE idempotency_key = ?", ("daily_canary:2026-08-06:abc",)
    ).fetchone()["n"]
    assert count == 1


def test_send_after_mark_attempting_updates_the_same_row_to_sent(conn):
    key = "daily_canary:2026-08-06:abc"
    assert mark_attempting(conn, key, 555, "daily_canary") is True

    sender = TelegramSender(bot_token=None, allowed_chat_ids=[555])
    result = asyncio.run(
        sender.send(
            conn, chat_id=555, text="hello", kind="daily_canary",
            idempotency_key=key, max_chars=4000, dry_run=True,
        )
    )

    assert result.already_sent is False
    row = conn.execute("SELECT * FROM deliveries WHERE idempotency_key = ?", (key,)).fetchone()
    assert row["status"] == "dry_run"
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE idempotency_key = ?", (key,)
    ).fetchone()["n"]
    assert count == 1
