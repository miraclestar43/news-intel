"""Gate A remediation Phase 2: event time must be validated source/event
time, never ingestion time. Gate A ran historical backfill content through
first_seen_at == ingestion instant, which would have let a two-year-old
article "count" as today's news purely because it was fetched today."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel.adapters.base import CollectedItem
from news_intel.event_repo import events_in_window, qualified_events_since
from news_intel.jobs.weekly import week_window
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.dedupe import upsert_raw_item
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from news_intel.pipeline.time_semantics import eligibility_at, resolve_anchor
from news_intel.settings import TimeSemanticsPolicy
from tests.conftest import insert_source_row


def _store(conn, config, *, source_id, title, excerpt, url, published_at, now):
    insert_source_row(conn, source_id, 0)
    item = CollectedItem(
        source_id=source_id, external_id=url, canonical_url=url, title=title,
        summary_excerpt=excerpt, language="en", published_at=published_at,
        origin_family=source_id, metadata={},
    )
    normalized = normalize(item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now)
    upsert = upsert_raw_item(conn, normalized)
    known = known_entities(config.topics.groups)
    event_id, is_new = find_or_create_event(
        conn, normalized, upsert.raw_item_id, config.policy.dedupe, known,
        now=now, time_policy=config.policy.time_semantics,
    )
    return event_id, is_new


def _event_row(conn, event_id):
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def test_two_year_old_article_imported_today_is_not_daily_eligible(config, conn):
    ingested_now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    two_years_ago = ingested_now - timedelta(days=730)

    event_id, _ = _store(
        conn, config, source_id="archive_feed",
        title="Old story about a regional trade dispute",
        excerpt="This ran two years before it was ever ingested by this pipeline.",
        url="https://archive.test/old-story", published_at=two_years_ago, now=ingested_now,
    )

    row = _event_row(conn, event_id)
    assert row["archive_only"] == 0  # it does have a real (old) published time
    assert row["published_anchor_at"] == two_years_ago.isoformat()
    assert row["first_seen_at"] == ingested_now.isoformat()  # ingestion time is today...

    # ...but it must not be eligible for a "since yesterday" daily window,
    # because eligibility is judged by the article's own old published time.
    window_start = ingested_now - timedelta(hours=24)
    qualified = qualified_events_since(conn, window_start=window_start, min_score=0.0)
    assert event_id not in {r["id"] for r in qualified}


def test_current_article_in_a_large_first_crawl_remains_eligible(config, conn):
    """A large first-run backlog crawl (bootstrap) must not blanket-suppress
    genuinely current articles just because they arrived alongside a lot of
    old backlog content."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    old_id, _ = _store(
        conn, config, source_id="bootstrap_feed", title="Backlog item from months ago",
        excerpt="Old backlog content discovered on the very first crawl of this source.",
        url="https://bootstrap.test/old", published_at=now - timedelta(days=200), now=now,
    )
    current_id, _ = _store(
        conn, config, source_id="bootstrap_feed", title="Genuinely current story from today",
        excerpt="This is breaking news discovered on the same first crawl as the backlog above.",
        url="https://bootstrap.test/current", published_at=now - timedelta(hours=2), now=now,
    )

    window_start = now - timedelta(hours=24)
    qualified_ids = {r["id"] for r in qualified_events_since(conn, window_start=window_start, min_score=0.0)}
    assert current_id in qualified_ids
    assert old_id not in qualified_ids


def test_undated_bootstrap_item_defaults_to_archive_only(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    event_id, _ = _store(
        conn, config, source_id="undated_feed", title="Undated archive entry with no publication date",
        excerpt="This feed entry carries no parseable published time at all.",
        url="https://undated.test/item", published_at=None, now=now,
    )

    row = _event_row(conn, event_id)
    assert row["archive_only"] == 1
    assert row["published_anchor_at"] is None
    assert eligibility_at(row) is None

    window_start = now - timedelta(hours=24)
    qualified_ids = {r["id"] for r in qualified_events_since(conn, window_start=window_start, min_score=0.0)}
    assert event_id not in qualified_ids  # archive-only: no alert, no digest


def test_future_or_malformed_timestamp_fails_closed_to_archive_only():
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    policy = TimeSemanticsPolicy()

    far_future = now + timedelta(days=10)
    resolved = resolve_anchor(far_future, now=now, policy=policy)
    assert resolved.published_anchor_at is None
    assert resolved.archive_only is True

    # A small amount of clock skew (well within tolerance) is still trusted.
    slight_skew = now + timedelta(minutes=5)
    resolved_ok = resolve_anchor(slight_skew, now=now, policy=policy)
    assert resolved_ok.published_anchor_at == slight_skew
    assert resolved_ok.archive_only is False


def test_refreshed_feed_timestamp_alone_does_not_touch_event_time_fields(config, conn):
    """An UNCHANGED re-fetch (same content_hash, only fetched_at/published_at
    metadata refreshed) never reaches find_or_create_event at all -- so it
    cannot reset published_anchor_at, content_changed_at, or archive_only."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    old_published = now - timedelta(days=400)
    event_id, _ = _store(
        conn, config, source_id="stale_feed", title="A story that ran over a year ago",
        excerpt="Original text of the story.",
        url="https://stale.test/story", published_at=old_published, now=now,
    )
    before = dict(_event_row(conn, event_id))

    # Simulate the source re-publishing identical content with only its own
    # feed-level timestamp refreshed -- upsert_raw_item alone (as collect.py
    # would call it) reports UNCHANGED and never calls find_or_create_event.
    later = now + timedelta(days=1)
    item = CollectedItem(
        source_id="stale_feed", external_id="https://stale.test/story",
        canonical_url="https://stale.test/story", title="A story that ran over a year ago",
        summary_excerpt="Original text of the story.", language="en",
        published_at=later, origin_family="stale_feed", metadata={},
    )
    normalized = normalize(item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=later)
    upsert = upsert_raw_item(conn, normalized)
    from news_intel.pipeline.dedupe import UpsertResult

    assert upsert.result == UpsertResult.UNCHANGED

    after = dict(_event_row(conn, event_id))
    assert after["published_anchor_at"] == before["published_anchor_at"]
    assert after["content_changed_at"] == before["content_changed_at"]
    assert after["archive_only"] == before["archive_only"]


def test_old_event_with_genuinely_material_update_becomes_newly_eligible(config, conn):
    """An old (even archive-only) event that later receives a genuinely new
    source item -- content_changed, not merely a timestamp refresh -- must
    become eligible again, without rewriting its original published time."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    event_id, _ = _store(
        conn, config, source_id="wire_a", title="Ongoing regulatory review of a merger",
        excerpt="Regulators opened a review of the proposed merger.",
        url="https://wire-a.test/merger-review", published_at=now - timedelta(days=100), now=now,
    )
    window_start = now - timedelta(hours=24)
    assert event_id not in {
        r["id"] for r in qualified_events_since(conn, window_start=window_start, min_score=0.0)
    }

    # A genuinely new development reported soon after (within the clustering
    # candidate window), even though the original event is from 100 days ago.
    later = now + timedelta(hours=2)
    second_id, is_new = _store(
        conn, config, source_id="wire_a", title="Ongoing regulatory review of a merger",
        excerpt="Regulators cleared the merger with conditions, a material update.",
        url="https://wire-a.test/merger-cleared", published_at=later, now=later,
    )
    assert second_id == event_id
    assert is_new is False

    row = _event_row(conn, event_id)
    assert row["published_anchor_at"] == (now - timedelta(days=100)).isoformat()  # unchanged original anchor
    assert row["content_changed_at"] == later.isoformat()

    later_window_start = later - timedelta(hours=24)
    assert event_id in {
        r["id"] for r in qualified_events_since(conn, window_start=later_window_start, min_score=0.0)
    }


def test_pacific_current_and_preceding_week_windows_use_published_time_not_ingestion_time(config, conn):
    """All events here are ingested at the exact same instant (a single bulk
    backfill run) -- only their validated published_anchor_at differs, so a
    correct current/prior-week split proves window membership follows
    source/event time, not first_seen_at."""
    pt = ZoneInfo(config.policy.timezone)
    sunday_pt = datetime(2026, 8, 2, 22, 0, tzinfo=pt)
    ingestion_instant = sunday_pt.astimezone(UTC)  # everything below is "discovered" at this one instant

    current_start, current_end = week_window(sunday_pt)
    prior_start, prior_end = current_start - timedelta(days=7), current_start

    just_before_boundary = current_start - timedelta(minutes=1)  # last instant of the prior PT week
    just_after_boundary = current_start + timedelta(minutes=1)  # first instant of the current PT week

    prior_week_id, _ = _store(
        conn, config, source_id="pt_boundary_feed", title="Regional utility files annual rate case",
        excerpt="A utility commission filing unrelated to the other seeded event here.",
        url="https://ptboundary.test/prior", published_at=just_before_boundary.astimezone(UTC),
        now=ingestion_instant,
    )
    current_week_id, _ = _store(
        conn, config, source_id="pt_boundary_feed", title="Central bank governor testifies before committee",
        excerpt="A committee hearing unrelated to the other seeded event here.",
        url="https://ptboundary.test/current", published_at=just_after_boundary.astimezone(UTC),
        now=ingestion_instant,
    )

    current_ids = {r["id"] for r in events_in_window(conn, window_start=current_start, window_end=current_end)}
    prior_ids = {r["id"] for r in events_in_window(conn, window_start=prior_start, window_end=prior_end)}

    assert current_week_id in current_ids
    assert current_week_id not in prior_ids
    assert prior_week_id in prior_ids
    assert prior_week_id not in current_ids
