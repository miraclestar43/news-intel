"""Explicit multipart delivery tracking for the weekly brief (2026-08-17).
See migrations/011_weekly_multipart_delivery.sql for the schema rationale.

This module ADDS a first-class "one logical weekly delivery, ordered parts"
record on top of the existing per-part `deliveries` idempotency mechanism
(delivery/telegram.py) -- it does not replace or change that mechanism,
which already durably prevents resending a successfully-sent part. This
module is what makes "was this week's delivery complete" and "what did
part 2 actually contain" answerable as one queryable unit, and is what
`weekly_deliveries.status` (pending/partial/sent/failed) records for a
partially-failed multipart send: NEVER auto-retried from here -- a later,
explicitly authorized recovery action is a separate, deliberate step."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

STATUS_PENDING = "pending"
STATUS_PARTIAL = "partial"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

PART_NOT_ATTEMPTED = "not_attempted"
PART_DRY_RUN = "dry_run"
PART_SENT = "sent"
PART_FAILED = "failed"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PartRecord:
    part_index: int
    text_hash: str
    telegram_message_id: str | None
    status: str


def get_or_create_delivery(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    chat_id_hash: str,
    period_label: str,
    total_parts: int,
    edition_id: str = "en_global",
    kind: str = "weekly",
    delivery_profile_id: str = "default",
) -> int:
    """Idempotent: a repeated call with the same idempotency_key returns
    the SAME logical delivery id rather than creating a duplicate row --
    this is what makes "repeated invocation must not duplicate part one or
    part two" hold at the logical-delivery level, not just per-part.
    `period_label` is an ISO week ("2026-W33") for kind='weekly' or a
    calendar month ("2026-08") for kind='monthly' -- this table (and
    `weekly_delivery_parts`) is shared by both digest kinds and both
    editions (2026-08-17 multi-edition architecture), despite the
    `weekly_`-prefixed table names, which are kept as a naming legacy from
    when only weekly existed."""
    existing = conn.execute(
        "SELECT id FROM weekly_deliveries WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if existing is not None:
        return existing["id"]
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO weekly_deliveries (idempotency_key, chat_id_hash, period_label, total_parts, status, "
        "created_at, updated_at, edition_id, kind, delivery_profile_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            idempotency_key, chat_id_hash, period_label, total_parts, STATUS_PENDING, now, now,
            edition_id, kind, delivery_profile_id,
        ),
    )
    return cur.lastrowid


def get_part(conn: sqlite3.Connection, weekly_delivery_id: int, part_index: int) -> PartRecord | None:
    row = conn.execute(
        "SELECT part_index, text_hash, telegram_message_id, status FROM weekly_delivery_parts "
        "WHERE weekly_delivery_id = ? AND part_index = ?",
        (weekly_delivery_id, part_index),
    ).fetchone()
    if row is None:
        return None
    return PartRecord(row["part_index"], row["text_hash"], row["telegram_message_id"], row["status"])


def record_part_attempt(
    conn: sqlite3.Connection,
    weekly_delivery_id: int,
    part_index: int,
    *,
    text: str,
    status: str,
    telegram_message_id: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    hashed = text_hash(text)
    existing = get_part(conn, weekly_delivery_id, part_index)
    if existing is None:
        conn.execute(
            "INSERT INTO weekly_delivery_parts (weekly_delivery_id, part_index, text_hash, "
            "telegram_message_id, status, attempted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (weekly_delivery_id, part_index, hashed, telegram_message_id, status, now),
        )
    else:
        conn.execute(
            "UPDATE weekly_delivery_parts SET text_hash = ?, telegram_message_id = ?, status = ?, "
            "attempted_at = ? WHERE weekly_delivery_id = ? AND part_index = ?",
            (hashed, telegram_message_id, status, now, weekly_delivery_id, part_index),
        )


def finalize_delivery(conn: sqlite3.Connection, weekly_delivery_id: int, status: str) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE weekly_deliveries SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, weekly_delivery_id),
    )


def all_parts(conn: sqlite3.Connection, weekly_delivery_id: int) -> list[PartRecord]:
    rows = conn.execute(
        "SELECT part_index, text_hash, telegram_message_id, status FROM weekly_delivery_parts "
        "WHERE weekly_delivery_id = ? ORDER BY part_index",
        (weekly_delivery_id,),
    ).fetchall()
    return [PartRecord(r["part_index"], r["text_hash"], r["telegram_message_id"], r["status"]) for r in rows]
