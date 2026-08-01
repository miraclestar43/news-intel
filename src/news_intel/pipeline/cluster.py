"""Stage 4: event clustering — group independent articles describing the
same real-world development into one event. Never clusters merely because
two stories mention the same company: a match requires title similarity OR
(trigram overlap AND entity overlap) together."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from news_intel.pipeline.entities import extract_entities
from news_intel.pipeline.normalize import NormalizedItem, normalize_title
from news_intel.pipeline.similarity import entity_overlap_ratio, title_similarity_score, trigram_jaccard
from news_intel.settings import DedupePolicy


def _next_event_id(conn: sqlite3.Connection, day: datetime) -> str:
    prefix = f"E-{day.strftime('%Y%m%d')}-"
    row = conn.execute(
        "SELECT id FROM events WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "%",)
    ).fetchone()
    seq = 1
    if row:
        seq = int(row["id"].rsplit("-", 1)[1]) + 1
    return f"{prefix}{seq:04d}"


def _candidate_events(conn: sqlite3.Connection, window_hours: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE status = 'developing' "
        "AND julianday('now') - julianday(last_updated_at) <= ? ",
        (window_hours / 24.0,),
    ).fetchall()


def find_or_create_event(
    conn: sqlite3.Connection,
    item: NormalizedItem,
    raw_item_id: int,
    dedupe_policy: DedupePolicy,
    known_entities: set[str],
    *,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Returns (event_id, is_new_event)."""
    now = now or datetime.now(UTC)
    item_entities = extract_entities(f"{item.title} {item.summary_excerpt or ''}", known_entities)

    best_match: sqlite3.Row | None = None
    for candidate in _candidate_events(conn, dedupe_policy.candidate_window_hours):
        cand_entities = json.loads(candidate["entities_json"] or "[]")
        cand_normalized_title = normalize_title(candidate["primary_title"])
        title_sim = title_similarity_score(item.normalized_title, cand_normalized_title)
        trigram = trigram_jaccard(item.normalized_title, cand_normalized_title)
        overlap = entity_overlap_ratio(item_entities, cand_entities)

        is_match = title_sim >= dedupe_policy.title_similarity_threshold or (
            trigram >= dedupe_policy.trigram_jaccard_threshold
            and overlap >= dedupe_policy.entity_overlap_threshold
        )
        if is_match:
            best_match = candidate
            break

    if best_match is not None:
        event_id = best_match["id"]
        merged_entities = sorted(set(item_entities) | set(json.loads(best_match["entities_json"] or "[]")))
        conn.execute(
            "UPDATE events SET last_updated_at = ?, entities_json = ? WHERE id = ?",
            (now.isoformat(), json.dumps(merged_entities, ensure_ascii=False), event_id),
        )
        _attach_source(conn, event_id, raw_item_id, is_primary=False)
        return event_id, False

    event_id = _next_event_id(conn, now)
    conn.execute(
        """
        INSERT INTO events (
            id, primary_title, status, confirmation_state,
            first_seen_at, last_updated_at, published_anchor_at, entities_json
        ) VALUES (?, ?, 'developing', 'unverified', ?, ?, ?, ?)
        """,
        (
            event_id,
            item.title,
            now.isoformat(),
            now.isoformat(),
            item.published_at.isoformat() if item.published_at else now.isoformat(),
            json.dumps(item_entities, ensure_ascii=False),
        ),
    )
    _attach_source(conn, event_id, raw_item_id, is_primary=True)
    return event_id, True


def _attach_source(conn: sqlite3.Connection, event_id: str, raw_item_id: int, *, is_primary: bool) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
        "VALUES (?, ?, ?, ?)",
        (event_id, raw_item_id, int(is_primary), "[]"),
    )
