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
from news_intel.pipeline.product_identity import extract_product_identity, has_conflicting_product_version
from news_intel.pipeline.relevance import is_legal_notice_boilerplate
from news_intel.pipeline.similarity import entity_overlap_ratio, title_similarity_score, trigram_jaccard
from news_intel.pipeline.time_semantics import resolve_anchor
from news_intel.settings import DedupePolicy, LegalNoticeBoilerplatePolicy, TimeSemanticsPolicy


def _next_event_id(conn: sqlite3.Connection, day: datetime) -> str:
    prefix = f"E-{day.strftime('%Y%m%d')}-"
    row = conn.execute(
        "SELECT id FROM events WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "%",)
    ).fetchone()
    seq = 1
    if row:
        seq = int(row["id"].rsplit("-", 1)[1]) + 1
    return f"{prefix}{seq:04d}"


def _candidate_events(
    conn: sqlite3.Connection, window_hours: int, *, now: datetime, edition_id: str
) -> list[sqlite3.Row]:
    """2026-08-17 multi-edition architecture: scoped to the SAME edition as
    the item being clustered -- an English and a Chinese raw_item
    describing the same real-world development must never merge into one
    event row (they belong to different editions' own independent
    selection/scoring/novelty pipelines; see migration 012's docstring).
    This is the actual mechanism that makes cross-edition event identity
    impossible, not just a convention."""
    return conn.execute(
        "SELECT * FROM events WHERE status = 'developing' AND edition_id = ? "
        "AND julianday(?) - julianday(last_updated_at) <= ? ",
        (edition_id, now.isoformat(), window_hours / 24.0),
    ).fetchall()


def find_or_create_event(
    conn: sqlite3.Connection,
    item: NormalizedItem,
    raw_item_id: int,
    dedupe_policy: DedupePolicy,
    known_entities: set[str],
    *,
    now: datetime | None = None,
    time_policy: TimeSemanticsPolicy | None = None,
    legal_notice_policy: LegalNoticeBoilerplatePolicy | None = None,
) -> tuple[str, bool]:
    """Returns (event_id, is_new_event)."""
    now = now or datetime.now(UTC)
    time_policy = time_policy or TimeSemanticsPolicy()
    legal_notice_policy = legal_notice_policy or LegalNoticeBoilerplatePolicy()
    item_text = f"{item.title} {item.summary_excerpt or ''}"
    item_entities = extract_entities(item_text, known_entities)
    # Gate A remediation Phase 3.4: templated law-firm shareholder-alert
    # boilerplate must never cluster purely on shared boilerplate phrasing
    # -- different tickers/defendants would otherwise merge because their
    # titles/trigrams are near-identical and their capitalized-phrase
    # fallback "entities" (e.g. "Law Firm", "Class Action") overlap. For
    # boilerplate text, require a shared *known-vocabulary* entity instead
    # of gross title/trigram similarity.
    item_is_boilerplate = is_legal_notice_boilerplate(item_text, legal_notice_policy)
    item_known_entities = {e for e in item_entities if e in known_entities}
    # Retry 1 Blocker 2: a high fuzzy-title score must never override an
    # explicit, conflicting product/version identifier -- "Introducing
    # GPT-5.5" and "Introducing GPT-5.4" are near-identical templated
    # announcements of two different real releases, not one event.
    item_identity = extract_product_identity(item_text)

    best_match: sqlite3.Row | None = None
    for candidate in _candidate_events(
        conn, dedupe_policy.candidate_window_hours, now=now, edition_id=item.edition_id
    ):
        cand_entities = json.loads(candidate["entities_json"] or "[]")

        if item_is_boilerplate:
            cand_known_entities = {e for e in cand_entities if e in known_entities}
            is_match = bool(item_known_entities & cand_known_entities)
        elif has_conflicting_product_version(item_identity, extract_product_identity(candidate["primary_title"])):
            is_match = False
        else:
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
        # Only NEW/CONTENT_CHANGED raw items ever reach find_or_create_event
        # (Phase 1.1 gates UNCHANGED out before this point), so reaching the
        # merge branch always means genuinely new/changed content -- bump
        # content_changed_at so this counts as a fresh material update for
        # digest/trend eligibility, without touching the event's original
        # published_anchor_at.
        conn.execute(
            "UPDATE events SET last_updated_at = ?, entities_json = ?, content_changed_at = ? WHERE id = ?",
            (now.isoformat(), json.dumps(merged_entities, ensure_ascii=False), now.isoformat(), event_id),
        )
        _attach_source(conn, event_id, raw_item_id, is_primary=False)
        return event_id, False

    anchor = resolve_anchor(item.published_at, now=now, policy=time_policy)
    event_id = _next_event_id(conn, now)
    conn.execute(
        """
        INSERT INTO events (
            id, primary_title, status, confirmation_state,
            first_seen_at, last_updated_at, published_anchor_at, archive_only, entities_json, edition_id
        ) VALUES (?, ?, 'developing', 'unverified', ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            item.title,
            now.isoformat(),
            now.isoformat(),
            anchor.published_anchor_at.isoformat() if anchor.published_anchor_at else None,
            int(anchor.archive_only),
            json.dumps(item_entities, ensure_ascii=False),
            item.edition_id,
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
    # Self-heals the 2026-08-06 incident's orphaned_at marker (see migration
    # 008): if this event was previously left with zero sources and is now
    # gaining one again, it is no longer orphaned. A cheap unconditional
    # UPDATE (no-op if already NULL) rather than a SELECT-then-maybe-UPDATE,
    # since _attach_source is the only other event_sources writer besides
    # collect.py's orphan-detection path and is called on every clustering
    # decision, not just recovery.
    conn.execute("UPDATE events SET orphaned_at = NULL WHERE id = ? AND orphaned_at IS NOT NULL", (event_id,))
