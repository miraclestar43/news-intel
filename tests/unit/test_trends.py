"""Phase 3 acceptance tests 2, 3, 8: window arithmetic, trend-strength
dominance resistance, and DST/ISO-week determinism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel.jobs.weekly import week_window
from news_intel.pipeline.trends import build_trend_clusters
from tests.conftest import insert_source_row

PT = ZoneInfo("America/Los_Angeles")


def test_week_window_is_monday_to_next_monday_pt():
    # 2026-08-02 is a Sunday.
    now_pt = datetime(2026, 8, 2, 22, 0, tzinfo=PT)
    start, end = week_window(now_pt)
    assert start == datetime(2026, 7, 27, 0, 0, tzinfo=PT)  # the preceding Monday
    assert end == datetime(2026, 8, 3, 0, 0, tzinfo=PT)  # the following Monday
    assert (end - start) == timedelta(days=7)


def test_prior_week_window_is_a_distinct_non_overlapping_pt_window():
    now_pt = datetime(2026, 8, 2, 22, 0, tzinfo=PT)
    current_start, current_end = week_window(now_pt)
    prior_start, prior_end = current_start - timedelta(days=7), current_start
    assert prior_end == current_start  # abut, never overlap
    assert prior_start < prior_end
    assert current_start < current_end


def test_week_window_deterministic_across_dst_spring_forward():
    # 2026 spring-forward is Sunday March 8. Both the Sunday before and the
    # Sunday of the transition must resolve to clean local midnight bounds.
    before = datetime(2026, 3, 1, 22, 0, tzinfo=PT)
    during = datetime(2026, 3, 8, 22, 0, tzinfo=PT)
    start_before, end_before = week_window(before)
    start_during, end_during = week_window(during)
    assert start_before.time() == datetime.min.time()
    assert end_before.time() == datetime.min.time()
    assert start_during.time() == datetime.min.time()
    assert end_during.time() == datetime.min.time()
    assert end_before == start_during  # consecutive weeks abut exactly

    iso_before = before.isocalendar()
    iso_during = during.isocalendar()
    assert iso_during.week == iso_before.week + 1


def _seed_trend_event(conn, *, event_id, title, category, score, confq, tier, origin_family, day_offset, now):
    ts = now - timedelta(days=day_offset)
    conn.execute(
        "INSERT INTO events (id, primary_title, category, first_seen_at, last_updated_at, "
        "confirmation_state, final_score, confirmation_quality, entities_json) "
        "VALUES (?, ?, ?, ?, ?, 'official', ?, ?, ?)",
        (event_id, title, category, ts.isoformat(), ts.isoformat(), score, confq, '["Shared Entity"]'),
    )
    insert_source_row(conn, f"src_{event_id}", tier)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (f"src_{event_id}", event_id, f"https://x.test/{event_id}", event_id, title, title.lower(),
         "excerpt", ts.isoformat(), event_id, origin_family),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )


def test_ten_syndicated_articles_do_not_beat_fewer_diverse_high_quality_events(conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    # One "trend": 10 events, all same origin_family (syndicated copies),
    # middling score, no persistence (all on the same day).
    for i in range(10):
        _seed_trend_event(
            conn, event_id=f"E-SYN-{i}", title=f"Syndicated story #{i}", category="US / China / World",
            score=60, confq=50, tier=2, origin_family="one-syndicate.test", day_offset=0, now=now,
        )

    # Another "trend": only 3 events, but from 3 independent origin
    # families, higher confirmation quality, spread across several days
    # (persistence) with one material update each.
    for i in range(3):
        event_id = f"E-DIV-{i}"
        _seed_trend_event(
            conn, event_id=event_id, title=f"Diverse independent story #{i}", category="AI & Semiconductors",
            score=75, confq=90, tier=0, origin_family=f"independent-{i}.gov", day_offset=i * 2, now=now,
        )
        conn.execute(
            "INSERT INTO event_updates (event_id, observed_at, novelty_score, changed_facts_json, "
            "previous_confirmation_state, new_confirmation_state) VALUES (?, ?, 0, '[]', 'reported', 'official')",
            (event_id, now.isoformat()),
        )

    all_events = conn.execute("SELECT * FROM events ORDER BY final_score DESC").fetchall()
    clusters = build_trend_clusters(conn, all_events, known_entities=set())

    syndicated_cluster = next(c for c in clusters if "US / China / World" == c.category)
    diverse_cluster = next(c for c in clusters if "AI & Semiconductors" == c.category)

    assert len(syndicated_cluster.event_ids) == 10
    assert len(diverse_cluster.event_ids) == 3
    assert diverse_cluster.strength > syndicated_cluster.strength
    assert diverse_cluster.distinct_origin_families > syndicated_cluster.distinct_origin_families
