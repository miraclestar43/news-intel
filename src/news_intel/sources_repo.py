"""Keeps the `sources` table in sync with config/sources.yaml and tracks
per-source health/cursor state. Isolated from adapters so tests can drive
health transitions without touching the network."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from news_intel.adapters.base import FetchOutcome, SourceHealthState
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


def record_outcome(conn: sqlite3.Connection, source_id: str, outcome: FetchOutcome) -> None:
    now = datetime.now(UTC).isoformat()
    if outcome.health_state == SourceHealthState.HEALTHY:
        conn.execute(
            """
            UPDATE sources SET last_cursor = ?, last_success_at = ?, last_attempt_at = ?,
                consecutive_failures = 0, health_state = ?, health_detail = ?
            WHERE id = ?
            """,
            (outcome.new_cursor, now, now, outcome.health_state.value, outcome.health_detail, source_id),
        )
    else:
        conn.execute(
            """
            UPDATE sources SET last_attempt_at = ?, consecutive_failures = consecutive_failures + 1,
                health_state = ?, health_detail = ?
            WHERE id = ?
            """,
            (now, outcome.health_state.value, outcome.health_detail, source_id),
        )


def mark_disabled(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        "UPDATE sources SET health_state = 'disabled', health_detail = 'disabled in config' WHERE id = ?",
        (source_id,),
    )


def health_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, tier, enabled, health_state, health_detail, last_success_at, "
        "consecutive_failures FROM sources ORDER BY id"
    ).fetchall()
