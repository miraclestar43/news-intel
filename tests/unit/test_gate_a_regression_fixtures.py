"""Gate A remediation Phase 3/5: end-to-end scoring regression fixtures for
the two documented Gate A false positives. These reproduce the actual
reported wording as test fixtures only -- production logic (topics.yaml,
pipeline/relevance.py) contains no source-specific or title-specific
special-casing; the fix is the generic word-boundary + contextual-tariff +
honest-unclassified-default logic exercised here."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.pipeline.scoring import persist_score, score_event
from tests.conftest import insert_source_row


def _make_event_with_source(conn, title, excerpt, tier=0, origin_family="gov.test"):
    now = datetime.now(UTC).isoformat()
    event_id = "E-20260731-0098"
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, 'unverified')",
        (event_id, title, now, now, now),
    )
    insert_source_row(conn, "src1", tier)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES ('src1', 'ext1', 'https://x.test/1', 'h1', ?, ?, ?, 'en', ?, 'ch1', ?)",
        (title, title.lower(), excerpt, now, origin_family),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )
    return event_id


def test_nagpra_notice_does_not_qualify_for_top3(config, conn):
    event_id = _make_event_with_source(
        conn,
        "Notice of Inventory Completion: NAGPRA repatriation of Native American human remains",
        "National Park Service completed an inventory of human remains under the Native "
        "American Graves Protection and Repatriation Act.",
        tier=0, origin_family="federalregister.gov",
    )
    scores = score_event(conn, event_id, config, is_new_event=True)
    weights = config.policy.scoring.weights.model_dump()
    final = persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)

    assert final < config.policy.scoring.thresholds.daily_top3
    category = conn.execute("SELECT category FROM events WHERE id = ?", (event_id,)).fetchone()["category"]
    assert category == "Unclassified"


def test_electronic_tariff_filings_notice_does_not_qualify_for_top3_or_ai_semiconductor(config, conn):
    event_id = _make_event_with_source(
        conn,
        "Electronic Tariff Filings docket update",
        "Carriers submitted revised electronic tariff filing schedules with the commission "
        "covering routine import duty administrative changes.",
        tier=0, origin_family="federalregister.gov",
    )
    scores = score_event(conn, event_id, config, is_new_event=True)
    weights = config.policy.scoring.weights.model_dump()
    final = persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)

    assert final < config.policy.scoring.thresholds.daily_top3
    category = conn.execute("SELECT category FROM events WHERE id = ?", (event_id,)).fetchone()["category"]
    assert category != "AI & Semiconductors"
