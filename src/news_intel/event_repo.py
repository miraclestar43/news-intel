"""Read helpers for turning stored events + their attached raw_items into the
plain dicts the LLM boundary and the deterministic fallback both consume."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def qualified_events_since(
    conn: sqlite3.Connection, *, window_start: datetime, min_score: float
) -> list[sqlite3.Row]:
    """`primary_origin_family` is the origin_family of the event's primary
    source (falling back to any attached source) — the unit that Phase 1
    hardening's per-digest diversity cap operates on, since two events can
    share a company name but come from genuinely different publishers.

    Window membership uses eligibility time (COALESCE(content_changed_at,
    published_anchor_at) -- Gate A remediation Phase 2), never ingestion
    time: an old article ingested today must not qualify just because
    first_seen_at/last_updated_at are recent, and archive-only events (NULL
    eligibility time) never qualify at all."""
    return conn.execute(
        """
        SELECT e.*,
            (
                SELECT r.origin_family FROM event_sources es
                JOIN raw_items r ON r.id = es.raw_item_id
                WHERE es.event_id = e.id
                ORDER BY es.is_primary DESC, r.id
                LIMIT 1
            ) AS primary_origin_family
        FROM events e
        WHERE e.final_score >= ?
          AND COALESCE(e.content_changed_at, e.published_anchor_at) >= ?
        ORDER BY e.final_score DESC
        """,
        (min_score, window_start.astimezone(UTC).isoformat()),
    ).fetchall()


def events_in_window(
    conn: sqlite3.Connection, *, window_start: datetime, window_end: datetime, min_score: float = 0.0
) -> list[sqlite3.Row]:
    """Bounded [window_start, window_end) pull for the weekly brief (Phase
    3) -- both the current and prior 7-day windows, and unlike
    qualified_events_since, deliberately includes low-scoring/disputed
    events too (min_score defaults to 0) so noise/unresolved tracking has
    something to look at. Uses eligibility time, not ingestion time (see
    qualified_events_since docstring / pipeline/time_semantics.py)."""
    return conn.execute(
        """
        SELECT * FROM events
        WHERE final_score >= ?
          AND COALESCE(content_changed_at, published_anchor_at) >= ?
          AND COALESCE(content_changed_at, published_anchor_at) < ?
        ORDER BY final_score DESC
        """,
        (min_score, window_start.astimezone(UTC).isoformat(), window_end.astimezone(UTC).isoformat()),
    ).fetchall()


def event_sources(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.id AS raw_item_id, r.title AS title, r.summary_excerpt AS excerpt,
               r.canonical_url AS url, r.published_at AS published_at,
               s.name AS publisher, s.tier AS tier
        FROM event_sources es
        JOIN raw_items r ON r.id = es.raw_item_id
        JOIN sources s ON s.id = r.source_id
        WHERE es.event_id = ?
        ORDER BY es.is_primary DESC, r.published_at
        """,
        (event_id,),
    ).fetchall()
    return [
        {
            "id": f"S{r['raw_item_id']}",
            "publisher": r["publisher"],
            "excerpt": r["excerpt"] or r["title"],
            "published_at": r["published_at"] or "",
            "url": r["url"],
            "tier": r["tier"],
        }
        for r in rows
    ]


def combined_text_for_event(conn: sqlite3.Connection, event_id: str) -> str:
    rows = conn.execute(
        """
        SELECT r.title AS title, r.summary_excerpt AS excerpt
        FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id
        WHERE es.event_id = ?
        """,
        (event_id,),
    ).fetchall()
    return " ".join(f"{r['title']} {r['excerpt'] or ''}" for r in rows)


def event_as_digest_dict(conn: sqlite3.Connection, event_row: sqlite3.Row) -> dict:
    sources = event_sources(conn, event_row["id"])
    return {
        "event_id": event_row["id"],
        "title": event_row["primary_title"],
        "category": event_row["category"] or "uncategorized",
        "excerpt": sources[0]["excerpt"] if sources else "",
        "sources": sources,
        "final_score": event_row["final_score"],
        "confirmation_state": event_row["confirmation_state"],
    }


def record_digest(
    conn: sqlite3.Connection,
    *,
    kind: str,
    window_start: datetime,
    window_end: datetime,
    rendered_text: str,
    model_used: str,
    fallback_used: bool,
    event_ids_top3: list[str],
    event_ids_quick: list[str],
) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO digests (kind, window_start, window_end, generation_state, rendered_text,
            model_used, fallback_used, created_at)
        VALUES (?, ?, ?, 'generated', ?, ?, ?, ?)
        """,
        (kind, window_start.isoformat(), window_end.isoformat(), rendered_text, model_used, int(fallback_used), now),
    )
    digest_id = cur.lastrowid
    for rank, event_id in enumerate(event_ids_top3, start=1):
        conn.execute(
            "INSERT INTO digest_events (digest_id, event_id, rank, section) VALUES (?, ?, ?, 'top3')",
            (digest_id, event_id, rank),
        )
    for rank, event_id in enumerate(event_ids_quick, start=1):
        conn.execute(
            "INSERT INTO digest_events (digest_id, event_id, rank, section) VALUES (?, ?, ?, 'quick_brief')",
            (digest_id, event_id, rank),
        )
    return digest_id


def create_digest(
    conn: sqlite3.Connection,
    *,
    kind: str,
    window_start: datetime,
    window_end: datetime,
    rendered_text: str,
    model_used: str,
    fallback_used: bool,
) -> int:
    """Generic digest-row creation for digests whose event references don't
    fit the daily job's fixed top3/quick_brief sections (e.g. weekly's
    multiple named sections) -- pair with add_digest_events()."""
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO digests (kind, window_start, window_end, generation_state, rendered_text,
            model_used, fallback_used, created_at)
        VALUES (?, ?, ?, 'generated', ?, ?, ?, ?)
        """,
        (kind, window_start.isoformat(), window_end.isoformat(), rendered_text, model_used, int(fallback_used), now),
    )
    return cur.lastrowid


def add_digest_events(conn: sqlite3.Connection, digest_id: int, event_ids: list[str], section: str) -> None:
    for rank, event_id in enumerate(event_ids, start=1):
        conn.execute(
            "INSERT OR IGNORE INTO digest_events (digest_id, event_id, rank, section) VALUES (?, ?, ?, ?)",
            (digest_id, event_id, rank, section),
        )


def last_digest_window_end(conn: sqlite3.Connection, kind: str) -> datetime | None:
    row = conn.execute(
        "SELECT window_end FROM digests WHERE kind = ? AND generation_state = 'generated' "
        "ORDER BY created_at DESC LIMIT 1",
        (kind,),
    ).fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row["window_end"])
