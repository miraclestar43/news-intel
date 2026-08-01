"""Calendar/ICS items (BLS/BEA/Fed release dates) feed `scheduled_events`,
used for 'Tomorrow to Watch' / 'Next Week' — they are not news events and
never enter dedupe/clustering/scoring."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from news_intel.adapters.base import CollectedItem


def record_scheduled_events(
    conn: sqlite3.Connection, source_id: str, items: list[CollectedItem], *, now: datetime | None = None
) -> int:
    now = (now or datetime.now(UTC)).isoformat()
    inserted = 0
    for item in items:
        if item.published_at is None:
            continue
        exists = conn.execute(
            "SELECT 1 FROM scheduled_events WHERE source_id = ? AND title = ? AND scheduled_at = ?",
            (source_id, item.title, item.published_at.isoformat()),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO scheduled_events (source_id, title, category, scheduled_at, detail, fetched_at, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, item.title, "calendar", item.published_at.isoformat(), item.summary_excerpt, now,
             item.canonical_url),
        )
        inserted += 1
    return inserted


def upcoming(
    conn: sqlite3.Connection, *, within_hours: int = 36, now: datetime | None = None
) -> list[sqlite3.Row]:
    now = now or datetime.now(UTC)
    cutoff = (now + timedelta(hours=within_hours)).isoformat()
    return conn.execute(
        "SELECT * FROM scheduled_events WHERE scheduled_at BETWEEN ? AND ? ORDER BY scheduled_at",
        (now.isoformat(), cutoff),
    ).fetchall()
