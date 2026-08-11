"""Telegram delivery: allowlist enforcement, section-safe splitting, and
durable send idempotency so a retried/duplicated job run never double-sends.

Real network sends are gated behind an explicit dry_run=False and a
configured bot token — Phase 1 defaults to dry-run until the user provides
and confirms a new bot token/chat id (design doc section 17, operating rule 10).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from news_intel.delivery.splitter import label_parts, split_at_sections


def chat_id_hash(chat_id: int) -> str:
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


@dataclass
class SendResult:
    idempotency_key: str
    already_sent: bool
    message_ids: list[str]
    dry_run: bool


def _get_delivery(conn: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM deliveries WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()


def mark_attempting(conn: sqlite3.Connection, idempotency_key: str, chat_id: int, kind: str) -> bool:
    """Durably pre-marks an idempotency key as attempted *before* any network
    call, so a send that raises (rather than returning a normal failure)
    still leaves a row behind -- without this, TelegramSender.send() only
    ever records "sent" or "dry_run" (see _record_delivery below), since a
    raised exception from bot.send_message() propagates before that call is
    reached. Callers that must never retry a failed/ambiguous real send
    (e.g. the daily canary) call this first: returns False if the key was
    already attempted (in any status) and the caller must not proceed;
    returns True and inserts a 'attempting' row otherwise. Uses autocommit
    (db.connect sets isolation_level=None), so the mark is durable even if
    the process crashes mid-send."""
    if _get_delivery(conn, idempotency_key) is not None:
        return False
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO deliveries (idempotency_key, chat_id_hash, kind, telegram_message_id, "
        "attempts, status, created_at, updated_at) VALUES (?, ?, ?, NULL, 0, 'attempting', ?, ?)",
        (idempotency_key, chat_id_hash(chat_id), kind, now, now),
    )
    return True


def _record_delivery(
    conn: sqlite3.Connection,
    idempotency_key: str,
    chat_hash: str,
    kind: str,
    status: str,
    message_ids: list[str],
) -> None:
    existing = _get_delivery(conn, idempotency_key)
    now = datetime.now(UTC).isoformat()
    joined = ",".join(message_ids)
    if existing:
        conn.execute(
            "UPDATE deliveries SET status = ?, telegram_message_id = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE idempotency_key = ?",
            (status, joined, now, idempotency_key),
        )
    else:
        conn.execute(
            "INSERT INTO deliveries (idempotency_key, chat_id_hash, kind, telegram_message_id, "
            "attempts, status, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (idempotency_key, chat_hash, kind, joined, status, now, now),
        )


class ChatNotAllowedError(Exception):
    pass


class TelegramSender:
    def __init__(self, bot_token: str | None, allowed_chat_ids: list[int]):
        self.bot_token = bot_token
        self.allowed_chat_ids = set(allowed_chat_ids)

    async def send(
        self,
        conn: sqlite3.Connection,
        *,
        chat_id: int,
        text: str,
        kind: str,
        idempotency_key: str,
        max_chars: int,
        dry_run: bool = True,
        parse_mode: str | None = None,
    ) -> SendResult:
        if chat_id not in self.allowed_chat_ids:
            raise ChatNotAllowedError(f"chat_id {chat_id} is not in the configured allowlist")

        existing = _get_delivery(conn, idempotency_key)
        if existing is not None:
            # A real send is authoritative regardless of this call's mode: it
            # already happened, full stop. A prior *dry_run* record, though,
            # must never suppress a subsequent real (dry_run=False) attempt --
            # a shadow/preview record is not a delivery and must not be able
            # to block production sending once real credentials are
            # configured. Repeated dry-run calls remain idempotent among
            # themselves (existing["status"] == "dry_run" and this call is
            # also dry_run).
            already_final = existing["status"] == "sent" or (existing["status"] == "dry_run" and dry_run)
            if already_final:
                return SendResult(
                    idempotency_key,
                    True,
                    (existing["telegram_message_id"] or "").split(",") if existing["telegram_message_id"] else [],
                    dry_run,
                )

        chunks = label_parts(split_at_sections(text, max_chars))

        if dry_run or not self.bot_token:
            placeholder_ids = [f"dry-{i}" for i in range(len(chunks))]
            _record_delivery(conn, idempotency_key, chat_id_hash(chat_id), kind, "dry_run", placeholder_ids)
            return SendResult(idempotency_key, False, placeholder_ids, True)

        from telegram import Bot  # imported lazily: not needed for dry-run/tests

        bot = Bot(token=self.bot_token)
        message_ids: list[str] = []
        for chunk in chunks:
            msg = await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
            message_ids.append(str(msg.message_id))
        _record_delivery(conn, idempotency_key, chat_id_hash(chat_id), kind, "sent", message_ids)
        return SendResult(idempotency_key, False, message_ids, False)
