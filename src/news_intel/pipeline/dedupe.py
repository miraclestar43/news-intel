"""Stage 3: raw-item upsert with explicit NEW / CONTENT_CHANGED / UNCHANGED
semantics (Gate A remediation Phase 1.1).

Identity (same real-world article) is (source_id, external_id) first, then
canonical URL hash. Content (title/excerpt) is compared separately via
content_hash. This separation is what lets a source refresh its feed
timestamp -- fetched_at/published_at alone -- without content_hash changing,
so a timestamp-only refresh is correctly UNCHANGED, never CONTENT_CHANGED
or NEW. A genuine content edit at the same identity updates the existing
row in place (never inserts a second row for the same article) and is
reported as CONTENT_CHANGED so the caller can reprocess it exactly once.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from news_intel.pipeline.normalize import NormalizedItem


class UpsertResult(StrEnum):
    NEW = "new"
    CONTENT_CHANGED = "content_changed"
    UNCHANGED = "unchanged"


@dataclass
class UpsertOutcome:
    raw_item_id: int
    result: UpsertResult


def find_existing_identity(conn: sqlite3.Connection, item: NormalizedItem) -> sqlite3.Row | None:
    """Same real-world article, regardless of whether its content changed.

    2026-08-21 repair: the url_hash fallback (and find_existing_by_content
    below) used to match across ANY edition, not just this item's own. Real
    production defect: gov_cn_policy (en_global, pre-existing) and
    gov_cn_policy_zh (zh_cn, added 2026-08-17) both scrape the identical
    gov.cn policy-index page. Since url_hash/content_hash are identical for
    the same article regardless of which edition's source fetched it first,
    gov_cn_policy_zh's items were silently matching gov_cn_policy's
    already-stored rows and returning UNCHANGED -- gov_cn_policy_zh
    structurally never produced its own zh_cn raw_items/events, no matter
    how many times it fetched. Editions are independent content pools by
    design (separate selection, separate delivery); scoping identity lookups
    to the item's own edition_id fixes this in general, for any current or
    future source pair that happens to share an upstream URL/content across
    editions -- the same-edition, cross-source-id dedup this function
    already does (e.g. two different English-edition sources both mirroring
    one GDELT story) is unaffected and still merges those correctly."""
    if item.external_id:
        row = conn.execute(
            "SELECT * FROM raw_items WHERE source_id = ? AND external_id = ?",
            (item.source_id, item.external_id),
        ).fetchone()
        if row:
            return row

    return conn.execute(
        "SELECT * FROM raw_items WHERE url_hash = ? AND edition_id = ? ORDER BY id LIMIT 1",
        (item.url_hash, item.edition_id),
    ).fetchone()


def find_existing_by_content(conn: sqlite3.Connection, item: NormalizedItem) -> sqlite3.Row | None:
    """Identical content re-appearing under a different identity within the
    SAME edition (e.g. a mirror republishing the same text) -- still not a
    new article. Scoped by edition_id (see find_existing_identity above) so
    this never suppresses a different edition's independent copy of the same
    underlying story."""
    return conn.execute(
        "SELECT * FROM raw_items WHERE content_hash = ? AND edition_id = ? ORDER BY id LIMIT 1",
        (item.content_hash, item.edition_id),
    ).fetchone()


def _insert_new(conn: sqlite3.Connection, item: NormalizedItem) -> int | None:
    cur = conn.execute(
        """
        INSERT INTO raw_items (
            source_id, external_id, canonical_url, url_hash, title, normalized_title,
            summary_excerpt, language, published_at, fetched_at, content_hash,
            origin_family, origin_url, origin_title, origin_published_at, edition_id, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            item.origin_url,
            item.origin_title,
            item.origin_published_at.isoformat() if item.origin_published_at else None,
            item.edition_id,
            json.dumps(item.metadata, ensure_ascii=False),
        ),
    )
    if cur.lastrowid and cur.rowcount:
        return cur.lastrowid
    return None


def _update_in_place(conn: sqlite3.Connection, raw_item_id: int, item: NormalizedItem) -> None:
    conn.execute(
        """
        UPDATE raw_items SET
            title = ?, normalized_title = ?, summary_excerpt = ?, content_hash = ?,
            published_at = ?, fetched_at = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            item.title,
            item.normalized_title,
            item.summary_excerpt,
            item.content_hash,
            item.published_at.isoformat() if item.published_at else None,
            item.fetched_at.isoformat(),
            json.dumps(item.metadata, ensure_ascii=False),
            raw_item_id,
        ),
    )


def upsert_raw_item(conn: sqlite3.Connection, item: NormalizedItem) -> UpsertOutcome:
    """Only NEW and CONTENT_CHANGED outcomes should ever be fed onward into
    normalization/clustering/confirmation/scoring by the caller -- UNCHANGED
    means this exact article, with this exact content, was already fully
    processed on a prior cycle."""
    identity_row = find_existing_identity(conn, item)
    if identity_row is not None:
        if identity_row["content_hash"] == item.content_hash:
            return UpsertOutcome(identity_row["id"], UpsertResult.UNCHANGED)
        _update_in_place(conn, identity_row["id"], item)
        return UpsertOutcome(identity_row["id"], UpsertResult.CONTENT_CHANGED)

    content_row = find_existing_by_content(conn, item)
    if content_row is not None:
        return UpsertOutcome(content_row["id"], UpsertResult.UNCHANGED)

    new_id = _insert_new(conn, item)
    if new_id is not None:
        return UpsertOutcome(new_id, UpsertResult.NEW)

    # ON CONFLICT DO NOTHING path (concurrent-ish re-run): someone else's
    # insert won the race; treat as UNCHANGED rather than assuming ours did.
    existing = find_existing_identity(conn, item)
    assert existing is not None, "insert reported no-op but no matching row found"
    return UpsertOutcome(existing["id"], UpsertResult.UNCHANGED)
