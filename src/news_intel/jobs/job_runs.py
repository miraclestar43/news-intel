from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def start(conn: sqlite3.Connection, job_name: str, edition_id: str | None = None) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO job_runs (job_name, started_at, status, edition_id) VALUES (?, ?, 'running', ?)",
        (job_name, now, edition_id),
    )
    return cur.lastrowid


def finish(conn: sqlite3.Connection, run_id: int, status: str, detail: dict) -> None:
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, detail_json = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), status, json.dumps(detail, ensure_ascii=False), run_id),
    )


def last_run(conn: sqlite3.Connection, job_name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM job_runs WHERE job_name = ? ORDER BY started_at DESC LIMIT 1", (job_name,)
    ).fetchone()
