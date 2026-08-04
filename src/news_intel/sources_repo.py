"""Keeps the `sources` table in sync with config/sources.yaml and tracks
per-source health/cursor/scheduling state. Isolated from adapters so tests
can drive health transitions without touching the network."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from news_intel.adapters.base import FetchOutcome, SourceHealthState
from news_intel.pipeline.schedule import compute_next_due_at, is_source_due
from news_intel.settings import SourceEntry, SourcesConfig


def _config_hash(entry: SourceEntry) -> str:
    return hashlib.sha256(entry.model_dump_json().encode()).hexdigest()


def sync_sources(conn: sqlite3.Connection, sources: SourcesConfig) -> None:
    for entry in sources.sources:
        new_hash = _config_hash(entry)
        existing = conn.execute("SELECT config_hash FROM sources WHERE id = ?", (entry.id,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO sources (
                    id, name, adapter_type, tier, enabled, url, category_hints_json,
                    poll_interval_seconds, health_state, config_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)
                """,
                (
                    entry.id,
                    entry.name,
                    entry.adapter,
                    entry.tier,
                    int(entry.enabled),
                    entry.url,
                    json.dumps(entry.category_hints, ensure_ascii=False),
                    entry.poll_interval_seconds,
                    new_hash,
                ),
            )
        elif existing["config_hash"] != new_hash:
            conn.execute(
                """
                UPDATE sources SET name = ?, adapter_type = ?, tier = ?, enabled = ?, url = ?,
                    category_hints_json = ?, poll_interval_seconds = ?, config_hash = ?
                WHERE id = ?
                """,
                (
                    entry.name,
                    entry.adapter,
                    entry.tier,
                    int(entry.enabled),
                    entry.url,
                    json.dumps(entry.category_hints, ensure_ascii=False),
                    entry.poll_interval_seconds,
                    new_hash,
                    entry.id,
                ),
            )


def get_cursor(conn: sqlite3.Connection, source_id: str) -> str | None:
    row = conn.execute("SELECT last_cursor FROM sources WHERE id = ?", (source_id,)).fetchone()
    return row["last_cursor"] if row else None


def get_scheduling_state(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT next_due_at, consecutive_failures, poll_interval_seconds FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()


def source_due(conn: sqlite3.Connection, source_id: str, *, now: datetime) -> bool:
    """A never-before-attempted source (next_due_at IS NULL) is always due."""
    row = get_scheduling_state(conn, source_id)
    if row is None:
        return True
    next_due_at = datetime.fromisoformat(row["next_due_at"]) if row["next_due_at"] else None
    return is_source_due(next_due_at=next_due_at, now=now)


def record_outcome(
    conn: sqlite3.Connection, source_id: str, outcome: FetchOutcome, *, now: datetime
) -> None:
    now_iso = now.isoformat()
    row = conn.execute(
        "SELECT consecutive_failures, poll_interval_seconds FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    poll_interval_seconds = row["poll_interval_seconds"] if row else 300
    prior_failures = row["consecutive_failures"] if row else 0
    is_healthy = outcome.health_state == SourceHealthState.HEALTHY
    new_failures = 0 if is_healthy else prior_failures + 1

    next_due_at = compute_next_due_at(
        now=now,
        poll_interval_seconds=poll_interval_seconds,
        is_healthy=is_healthy,
        consecutive_failures=new_failures,
        retry_after_seconds=outcome.retry_after_seconds,
    )

    if is_healthy:
        conn.execute(
            """
            UPDATE sources SET last_cursor = ?, last_success_at = ?, last_attempt_at = ?,
                consecutive_failures = 0, health_state = ?, health_detail = ?, next_due_at = ?
            WHERE id = ?
            """,
            (
                outcome.new_cursor, now_iso, now_iso, outcome.health_state.value, outcome.health_detail,
                next_due_at.isoformat(), source_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE sources SET last_attempt_at = ?, consecutive_failures = ?,
                health_state = ?, health_detail = ?, next_due_at = ?
            WHERE id = ?
            """,
            (
                now_iso, new_failures, outcome.health_state.value, outcome.health_detail,
                next_due_at.isoformat(), source_id,
            ),
        )


def mark_disabled(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        "UPDATE sources SET health_state = 'disabled', health_detail = 'disabled in config' WHERE id = ?",
        (source_id,),
    )


def health_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, tier, enabled, health_state, health_detail, last_success_at, "
        "consecutive_failures, next_due_at FROM sources ORDER BY id"
    ).fetchall()
