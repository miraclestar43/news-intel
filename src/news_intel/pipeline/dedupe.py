"""Stage 3: exact dedupe against raw_items — external ID, canonical URL hash,
and content hash, in that order. Returns the existing row instead of
inserting a duplicate, so re-running collection is idempotent."""

from __future__ import annotations

import json
import sqlite3

from news_intel.pipeline.normalize import NormalizedItem


def find_existing(conn: sqlite3.Connection, item: NormalizedItem) -> sqlite3.Row | None:
    if item.external_id:
        row = conn.execute(
            "SELECT * FROM raw_items WHERE source_id = ? AND external_id = ?",
            (item.source_id, item.external_id),
        ).fetchone()
        if row:
            return row

    row = conn.execute(
        "SELECT * FROM raw_items WHERE url_hash = ? ORDER BY id LIMIT 1", (item.url_hash,)
    ).fetchone()
    if row:
        return row

    row = conn.execute(
        "SELECT * FROM raw_items WHERE content_hash = ? ORDER BY id LIMIT 1",
        (item.content_hash,),
    ).fetchone()
    return row


def insert_raw_item(conn: sqlite3.Connection, item: NormalizedItem) -> int:
    """Insert, or return the id of the pre-existing exact duplicate."""
    existing = find_existing(conn, item)
    if existing is not None:
        return existing["id"]

    cur = conn.execute(
        """
        INSERT INTO raw_items (
            source_id, external_id, canonical_url, url_hash, title, normalized_title,
            summary_excerpt, language, published_at, fetched_at, content_hash,
            origin_family, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, external_id) DO NOTHING
        """,
        (
            item.source_id,
            item.external_id,
            item.canonical_url,
            item.url_hash,
            item.title,
            item.normalized_title,
            item.summary_excerpt,
            item.language,
            item.published_at.isoformat() if item.published_at else None,
            item.fetched_at.isoformat(),
            item.content_hash,
            item.origin_family,
            json.dumps(item.metadata, ensure_ascii=False),
        ),
    )
    if cur.lastrowid and cur.rowcount:
        return cur.lastrowid

    # ON CONFLICT DO NOTHING path (concurrent-ish re-run): fetch the row that
    # already exists rather than assuming our insert won the race.
    existing = find_existing(conn, item)
    assert existing is not None, "insert reported no-op but no matching row found"
    return existing["id"]
