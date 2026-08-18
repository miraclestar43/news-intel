"""Explicit multipart delivery tracking for the monthly brief (2026-08-18).
See migrations/012_monthly_multipart_delivery.sql for the schema rationale
-- the identical shape and guarantees as delivery/weekly_delivery_repo.py,
just for the monthly cadence's own idempotency-key namespace."""

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
    conn: sqlite3.Connection, *, idempotency_key: str, chat_id_hash: str, month_label: str, total_parts: int
) -> int:
    """Idempotent: a repeated call with the same idempotency_key returns
    the SAME logical delivery id rather than creating a duplicate row."""
    existing = conn.execute(
        "SELECT id FROM monthly_deliveries WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if existing is not None:
        return existing["id"]
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO monthly_deliveries (idempotency_key, chat_id_hash, month_label, total_parts, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (idempotency_key, chat_id_hash, month_label, total_parts, STATUS_PENDING, now, now),
    )
    return cur.lastrowid


def get_part(conn: sqlite3.Connection, monthly_delivery_id: int, part_index: int) -> PartRecord | None:
    row = conn.execute(
        "SELECT part_index, text_hash, telegram_message_id, status FROM monthly_delivery_parts "
        "WHERE monthly_delivery_id = ? AND part_index = ?",
        (monthly_delivery_id, part_index),
    ).fetchone()
    if row is None:
        return None
    return PartRecord(row["part_index"], row["text_hash"], row["telegram_message_id"], row["status"])


def record_part_attempt(
    conn: sqlite3.Connection,
    monthly_delivery_id: int,
    part_index: int,
    *,
    text: str,
    status: str,
    telegram_message_id: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    hashed = text_hash(text)
    existing = get_part(conn, monthly_delivery_id, part_index)
    if existing is None:
        conn.execute(
            "INSERT INTO monthly_delivery_parts (monthly_delivery_id, part_index, text_hash, "
            "telegram_message_id, status, attempted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (monthly_delivery_id, part_index, hashed, telegram_message_id, status, now),
        )
    else:
        conn.execute(
            "UPDATE monthly_delivery_parts SET text_hash = ?, telegram_message_id = ?, status = ?, "
            "attempted_at = ? WHERE monthly_delivery_id = ? AND part_index = ?",
            (hashed, telegram_message_id, status, now, monthly_delivery_id, part_index),
        )


def finalize_delivery(conn: sqlite3.Connection, monthly_delivery_id: int, status: str) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE monthly_deliveries SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, monthly_delivery_id),
    )


def all_parts(conn: sqlite3.Connection, monthly_delivery_id: int) -> list[PartRecord]:
    rows = conn.execute(
        "SELECT part_index, text_hash, telegram_message_id, status FROM monthly_delivery_parts "
        "WHERE monthly_delivery_id = ? ORDER BY part_index",
        (monthly_delivery_id,),
    ).fetchall()
    return [PartRecord(r["part_index"], r["text_hash"], r["telegram_message_id"], r["status"]) for r in rows]
