"""DB access for alert_shadow_log -- the strictly observational record of
every breaking-alert decision-engine candidate/outcome. Kept separate from
alert_queue_repo.py (the real decision-state table) on purpose: nothing in
this module is read back by the decision engine (jobs/alerts.py), so it can
never influence eligibility/cooldown/budget/spacing -- it only records what
that engine already decided."""

from __future__ import annotations

import sqlite3
from datetime import datetime


def last_shadow_row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alert_shadow_log WHERE event_id = ? ORDER BY id DESC LIMIT 1",
        (event_id,),
    ).fetchone()


def record_shadow_observation(
    conn: sqlite3.Connection,
    *,
    evaluated_at: datetime,
    run_kind: str,
    event_id: str,
    primary_title: str | None,
    category: str | None,
    severity: str | None,
    trigger_id: str | None,
    is_statement_only: bool,
    action: str,
    reason: str | None,
    message_hash: str | None,
    eligible_at: datetime | None,
    would_have_sent: bool,
    hypothetical_rendered_text: str | None,
    source_urls_json: str,
    now: datetime,
) -> int:
    cur = conn.execute(
        "INSERT INTO alert_shadow_log (evaluated_at, run_kind, event_id, primary_title, category, "
        "severity, trigger_id, is_statement_only, action, reason, message_hash, eligible_at, "
        "would_have_sent, hypothetical_rendered_text, source_urls_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evaluated_at.isoformat(),
            run_kind,
            event_id,
            primary_title,
            category,
            severity,
            trigger_id,
            int(is_statement_only),
            action,
            reason,
            message_hash,
            eligible_at.isoformat() if eligible_at else None,
            int(would_have_sent),
            hypothetical_rendered_text,
            source_urls_json,
            now.isoformat(),
        ),
    )
    return cur.lastrowid


def all_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alert_shadow_log ORDER BY id").fetchall()


def would_send_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM alert_shadow_log WHERE would_have_sent = 1"
    ).fetchone()["n"]
