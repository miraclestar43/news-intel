"""Phase 1 hardening: one T0 source supplying many high-scoring candidates
must not crowd out qualified events from other origin families/categories.

Seeds events with explicit final_score/category/origin_family (bypassing the
real scoring heuristic) so the selection policy in pipeline/select.py is
exercised deterministically and in isolation from scoring calibration."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from news_intel.jobs.daily import run_daily
from tests.conftest import insert_source_row


def _seed_explicit_event(conn, *, event_id, title, category, final_score, source_id, tier, origin_family, now):
    conn.execute(
        "INSERT INTO events (id, primary_title, category, first_seen_at, last_updated_at, "
        "confirmation_state, final_score, severity) VALUES (?, ?, ?, ?, ?, 'official', ?, 'S1')",
        (event_id, title, category, now.isoformat(), now.isoformat(), final_score),
    )
    insert_source_row(conn, source_id, tier)
    conn.execute("UPDATE sources SET url = 'https://example.test' WHERE id = ?", (source_id,))
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (
            source_id, event_id, f"https://x.test/{event_id}", event_id, title, title.lower(),
            f"Details about {title}.", now.isoformat(), event_id, origin_family,
        ),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )


def _seed_dominance_scenario(conn, now):
    # One prolific T0 source: 6 qualified, all individually high-scoring.
    for i, score in enumerate([80, 79, 78, 77, 76, 75]):
        _seed_explicit_event(
            conn,
            event_id=f"E-DOM-PROLIFIC-{i:02d}",
            title=f"Prolific source story #{i}",
            category="AI & Semiconductors",
            final_score=score,
            source_id="prolific_source",
            tier=0,
            origin_family="prolific.test",
            now=now,
        )

    # A different AI/semiconductor family, lower score than several deferred
    # prolific stories but from a genuinely independent publisher.
    _seed_explicit_event(
        conn, event_id="E-DOM-TSMC", title="TSMC packaging capacity expansion",
        category="AI & Semiconductors", final_score=72.5, source_id="tsmc_source",
        tier=0, origin_family="tsmc.test", now=now,
    )

    # Two markets/economy events from one more independent family.
    for i, score in enumerate([74, 73]):
        _seed_explicit_event(
            conn, event_id=f"E-DOM-FED-{i:02d}", title=f"Fed macro release #{i}",
            category="Markets & Economy", final_score=score, source_id="fed_source",
            tier=0, origin_family="fed.gov", now=now,
        )

    # One world/geopolitics event, quick-brief tier only.
    _seed_explicit_event(
        conn, event_id="E-DOM-GEO", title="Geopolitical development abroad",
        category="US / China / World", final_score=60, source_id="geo_source",
        tier=0, origin_family="geo.test", now=now,
    )


def test_dominant_source_does_not_crowd_out_digest(config, conn):
    now = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    _seed_dominance_scenario(conn, now)

    summary = run_daily(config, conn, dry_run=True, now=now)

    total_events = summary.top3_count + summary.quick_brief_count
    assert 4 <= total_events <= 8
    assert summary.fallback_used is True  # no LLM configured in the `config` fixture

    digest_events = conn.execute(
        """
        SELECT e.id, e.category,
            (SELECT r.origin_family FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id
             WHERE es.event_id = e.id LIMIT 1) AS origin_family
        FROM digest_events de JOIN events e ON e.id = de.event_id
        WHERE de.digest_id = (SELECT id FROM digests ORDER BY id DESC LIMIT 1)
        """
    ).fetchall()

    assert len(digest_events) == total_events

    family_counts: dict[str, int] = {}
    for row in digest_events:
        family_counts[row["origin_family"]] = family_counts.get(row["origin_family"], 0) + 1

    # The dominant family supplied 6 qualified candidates but must not exceed
    # the configured per-family cap in the final digest...
    assert family_counts.get("prolific.test", 0) <= config.policy.digests.max_events_per_origin_family
    # ...while genuinely independent families that were *not* individually
    # higher-scoring than several deferred prolific stories still made it in.
    assert family_counts.get("fed.gov", 0) >= 1
    assert family_counts.get("tsmc.test", 0) >= 1
    assert family_counts.get("geo.test", 0) >= 1

    categories = {row["category"] for row in digest_events}
    assert "Markets & Economy" in categories
    assert "AI & Semiconductors" in categories

    # Evidence: the rendered fallback brief itself stays legible and diverse
    # (count distinct story numbers, since each event's own excerpt text also
    # repeats its title -- a fixture artifact, not double-inclusion).
    distinct_prolific_mentions = set(re.findall(r"Prolific source story #(\d)", summary.rendered_text))
    assert len(distinct_prolific_mentions) <= 2
    print("\n--- dominance-test fallback digest ---\n")
    print(summary.rendered_text)
