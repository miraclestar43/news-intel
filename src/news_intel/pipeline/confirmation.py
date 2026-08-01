"""Stage 5: deterministic confirmation-state assignment.

unverified -> reported -> confirmed -> official, with `disputed` reachable
from any state (disputed is only ever set explicitly, e.g. by a future
correction feed; rule scoring never guesses at it). The LLM never sets this
field — only this module does, from the tiers/origin-families of sources
actually attached to the event.

Two outlets reproducing the same wire copy count as one source family, so
`origin_family` diversity — not raw article count — drives corroboration.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from news_intel.models import ConfirmationState


def attached_source_signals(conn: sqlite3.Connection, event_id: str) -> tuple[bool, set[str], bool]:
    """Returns (has_t0, t1_origin_families, has_t1_or_t2). T3 alone must never
    advance past unverified — it can discover a candidate but never confirm it."""
    rows = conn.execute(
        """
        SELECT s.tier AS tier, r.origin_family AS origin_family
        FROM event_sources es
        JOIN raw_items r ON r.id = es.raw_item_id
        JOIN sources s ON s.id = r.source_id
        WHERE es.event_id = ?
        """,
        (event_id,),
    ).fetchall()

    has_t0 = any(r["tier"] == 0 for r in rows)
    t1_families = {r["origin_family"] for r in rows if r["tier"] == 1}
    has_t1_or_t2 = any(r["tier"] in (1, 2) for r in rows)
    return has_t0, t1_families, has_t1_or_t2


def compute_confirmation_state(
    conn: sqlite3.Connection, event_id: str, current: ConfirmationState
) -> ConfirmationState:
    if current == ConfirmationState.DISPUTED:
        return current  # only lifted by explicit correction handling, not here

    has_t0, t1_families, has_t1_or_t2 = attached_source_signals(conn, event_id)

    if has_t0:
        return ConfirmationState.OFFICIAL
    if len(t1_families) >= 2:
        return ConfirmationState.CONFIRMED
    if has_t1_or_t2:
        return ConfirmationState.REPORTED
    return ConfirmationState.UNVERIFIED


def update_confirmation_state(
    conn: sqlite3.Connection, event_id: str, *, now: datetime | None = None
) -> ConfirmationState:
    now = now or datetime.now(UTC)
    row = conn.execute(
        "SELECT confirmation_state FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    current = ConfirmationState(row["confirmation_state"])
    new_state = compute_confirmation_state(conn, event_id, current)
    if new_state != current:
        conn.execute(
            "UPDATE events SET confirmation_state = ? WHERE id = ?", (new_state.value, event_id)
        )
        conn.execute(
            "INSERT INTO event_updates (event_id, observed_at, novelty_score, changed_facts_json, "
            "previous_confirmation_state, new_confirmation_state) "
            "VALUES (?, ?, 0, '[]', ?, ?)",
            (event_id, now.isoformat(), current.value, new_state.value),
        )
    return new_state
