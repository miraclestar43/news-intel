"""Read helpers for turning stored events + their attached raw_items into the
plain dicts the LLM boundary and the deterministic fallback both consume."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from news_intel.pipeline.citation import resolve_citation_publisher


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
    eligibility time) never qualify at all.

    2026-08-06 incident: an event can end up with zero event_sources rows
    (jobs/collect.py's CONTENT_CHANGED path reclustered every one of its
    raw items onto other events -- see migration 008's orphaned_at column),
    yet keep a high, frozen, now-unbacked score and stay eligible by every
    other filter here. The EXISTS clause below is the authoritative,
    self-correcting check for that (a live read of event_sources, not the
    denormalized orphaned_at flag, so it can never drift out of sync) --
    daily candidate selection must only ever consider source-backed events."""
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
          AND EXISTS (SELECT 1 FROM event_sources es2 WHERE es2.event_id = e.id)
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


# Sources whose adapter stores the *original* publisher's own article URL
# and domain as canonical_url/origin_family (verified against
# adapters/gdelt.py: canonical_url is the article's own url, origin_family
# is the article's own site domain -- never GDELT's own site) -- for these,
# the discovery aggregator's static source name ("GDELT DOC API") is
# replaced with the resolved original-publisher domain for display.
_DISCOVERY_PASSTHROUGH_SOURCE_IDS = frozenset({"gdelt_doc"})

# Techmeme's canonical_url points at Techmeme's own roundup page, not the
# original article -- so unlike GDELT, there's no URL to resolve. But its
# excerpt reliably starts with a "Publication:" or "Author / Publication:"
# citation (pipeline/citation.py, parsed from Techmeme's own text, never
# invented) -- 2026-08-13 fix: displaying "Source: Techmeme" on every item
# both hid the real publisher (Wired, Bloomberg, ...) and, more materially,
# let two DIFFERENT real publishers both discovered via Techmeme collide
# under one misleading "Techmeme" label.
_CITATION_PASSTHROUGH_SOURCE_IDS = frozenset({"techmeme"})


def _display_publisher(source_id: str, origin_family: str | None, source_name: str, excerpt: str | None) -> str:
    if source_id in _DISCOVERY_PASSTHROUGH_SOURCE_IDS and origin_family:
        return origin_family.removeprefix("www.")
    if source_id in _CITATION_PASSTHROUGH_SOURCE_IDS:
        resolved = resolve_citation_publisher(excerpt)
        if resolved:
            return resolved
    return source_name


def event_sources(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.id AS raw_item_id, r.title AS title, r.summary_excerpt AS excerpt,
               r.canonical_url AS url, r.published_at AS published_at,
               r.origin_family AS origin_family, r.source_id AS source_id,
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
            "publisher": _display_publisher(r["source_id"], r["origin_family"], r["publisher"], r["excerpt"]),
            "source_id": r["source_id"],
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
    try:
        entities = json.loads(event_row["entities_json"] or "[]")
    except (json.JSONDecodeError, IndexError, KeyError):
        entities = []
    return {
        "event_id": event_row["id"],
        "title": event_row["primary_title"],
        "category": event_row["category"] or "uncategorized",
        "excerpt": sources[0]["excerpt"] if sources else "",
        "sources": sources,
        "final_score": event_row["final_score"],
        "confirmation_state": event_row["confirmation_state"],
        "entities": entities,
    }


def recent_digest_events(
    conn: sqlite3.Connection, *, since: datetime, until: datetime | None = None, kind: str = "daily"
) -> list[dict]:
    """Distinct events delivered in any `kind` digest with window_end >=
    `since` -- the cross-day novelty lookback pool (2026-08-12 content-
    quality repair). Ordered most-recent-first so a candidate's closest
    prior appearance is compared first.

    `until` (2026-08-13 hardening) bounds the pool to digests whose
    window_end <= until -- i.e. digests that had *already finished their
    own window* by the time this selection is happening. jobs/daily.py
    always passes until=window_start (this run's own window boundary),
    which structurally cannot include this same run's own digest (not yet
    created in normal operation) or any digest whose window overlaps or
    follows it. Without this bound, a read-only replay run *after* the
    target day's digest already exists in the database would see that same
    day's own delivered stories as "prior coverage" and wrongly suppress
    them as repeats of themselves -- self-contamination, not a real
    novelty result. Defaults to None (no upper bound) only for callers that
    intentionally want the unbounded view."""
    query = """
        SELECT DISTINCT e.*, d.window_end AS digest_window_end
        FROM digest_events de
        JOIN digests d ON d.id = de.digest_id
        JOIN events e ON e.id = de.event_id
        WHERE d.kind = ? AND d.window_end >= ?
    """
    params: list = [kind, since.isoformat()]
    if until is not None:
        query += " AND d.window_end <= ?"
        params.append(until.isoformat())
    query += " ORDER BY d.window_end DESC"
    rows = conn.execute(query, params).fetchall()
    return [event_as_digest_dict(conn, r) for r in rows]


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
