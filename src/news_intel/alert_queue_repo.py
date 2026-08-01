"""DB access for the alert_queue table -- cooldown lookups, spacing lookups,
daily budget counting, and state writes. Kept separate from the eligibility
gate (pipeline/alerts.py) and the quiet-hours arithmetic (pipeline/
alert_policy.py) so each piece is independently testable."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime


def last_sent_alert_for_event(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alert_queue WHERE event_id = ? AND state = 'sent' ORDER BY sent_at DESC LIMIT 1",
        (event_id,),
    ).fetchone()


def last_sent_s2_excluding_event(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alert_queue WHERE severity = 'S2' AND state = 'sent' AND event_id != ? "
        "ORDER BY sent_at DESC LIMIT 1",
        (event_id,),
    ).fetchone()


def count_sent_s2_on_pt_date(conn: sqlite3.Connection, pt_day: date, tz_name: str) -> int:
    """Counts distinct *messages*, not rows: a consolidated overnight release
    shares one message_hash across all its constituent alerts and must count
    as one message toward the daily S2 budget (design doc section 3.3)."""
    rows = conn.execute(
        "SELECT id, message_hash, sent_at FROM alert_queue "
        "WHERE severity = 'S2' AND state = 'sent' AND sent_at IS NOT NULL"
    ).fetchall()

    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    message_keys: set[str] = set()
    for row in rows:
        sent_at = datetime.fromisoformat(row["sent_at"])
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        if sent_at.astimezone(tz).date() == pt_day:
            message_keys.add(row["message_hash"] or f"id:{row['id']}")
    return len(message_keys)


def record_alert(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    severity: str,
    state: str,
    reason: str,
    now: datetime,
    eligible_at: datetime | None = None,
    sent_at: datetime | None = None,
    message_hash: str | None = None,
    material_snapshot_json: str | None = None,
    material_version_hash: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO alert_queue (event_id, severity, state, reason, eligible_at, created_at, sent_at, "
        "message_hash, material_snapshot_json, material_version_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            severity,
            state,
            reason,
            (eligible_at or now).isoformat(),
            now.isoformat(),
            sent_at.isoformat() if sent_at else None,
            message_hash,
            material_snapshot_json,
            material_version_hash,
        ),
    )
    return cur.lastrowid


def held_alerts_due(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alert_queue WHERE state = 'held' AND eligible_at <= ? ORDER BY created_at",
        (now.isoformat(),),
    ).fetchall()


def mark_state(conn: sqlite3.Connection, alert_id: int, state: str, *, sent_at: datetime | None = None) -> None:
    conn.execute(
        "UPDATE alert_queue SET state = ?, sent_at = ? WHERE id = ?",
        (state, sent_at.isoformat() if sent_at else None, alert_id),
    )


def release_as_batch(conn: sqlite3.Connection, alert_ids: list[int], *, message_hash: str, sent_at: datetime) -> None:
    for alert_id in alert_ids:
        conn.execute(
            "UPDATE alert_queue SET state = 'sent', sent_at = ?, message_hash = ? WHERE id = ?",
            (sent_at.isoformat(), message_hash, alert_id),
        )


# --- S2 burst-merge (10-minute coalescing window) --------------------------


def find_open_merge_batch(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """An S2 burst-merge batch that a new candidate can still join: it has
    at least one 'pending_merge' member whose window (eligible_at) has not
    yet closed."""
    return conn.execute(
        "SELECT * FROM alert_queue WHERE state = 'pending_merge' AND eligible_at > ? "
        "ORDER BY created_at DESC LIMIT 1",
        (now.isoformat(),),
    ).fetchone()


def pending_merge_batches_due(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Distinct message_hash values for pending_merge batches whose window
    has elapsed and are ready to finalize into one delivery."""
    rows = conn.execute(
        "SELECT DISTINCT message_hash FROM alert_queue WHERE state = 'pending_merge' AND eligible_at <= ?",
        (now.isoformat(),),
    ).fetchall()
    return [r["message_hash"] for r in rows]


def batch_members(conn: sqlite3.Connection, message_hash: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alert_queue WHERE message_hash = ? AND state = 'pending_merge' ORDER BY created_at",
        (message_hash,),
    ).fetchall()


def finalize_merge_batch(conn: sqlite3.Connection, message_hash: str, *, sent_at: datetime) -> None:
    conn.execute(
        "UPDATE alert_queue SET state = 'sent', sent_at = ? WHERE message_hash = ? AND state = 'pending_merge'",
        (sent_at.isoformat(), message_hash),
    )
