"""Tiny key/value store backing Telegram query-command state (currently just
/mute). Never used for anything security- or secret-sensitive."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_value(conn: sqlite3.Connection, key: str, value: str, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    conn.execute(
        "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now.isoformat()),
    )


MUTE_UNTIL_KEY = "mute_until"


def is_muted(conn: sqlite3.Connection, now: datetime) -> bool:
    value = get(conn, MUTE_UNTIL_KEY)
    if not value:
        return False
    until = datetime.fromisoformat(value)
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return now < until
