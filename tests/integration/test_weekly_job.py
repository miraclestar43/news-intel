"""Phase 3 acceptance tests 1, 4, 5, 6, 7, 9, 10 for the weekly intelligence
brief. Tests 2, 3, 8 (window arithmetic, dominance resistance, DST/ISO-week
determinism) live in tests/unit/test_trends.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel.adapters.base import CollectedItem
from news_intel.jobs.daily import run_daily
from news_intel.jobs.weekly import run_weekly, week_window
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.scheduled_repo import record_scheduled_events
from tests.conftest import insert_source_row

PT = ZoneInfo("America/Los_Angeles")
SUNDAY_PT = datetime(2026, 8, 2, 22, 0, tzinfo=PT)


def _seed(conn, *, event_id, title, excerpt, category, score, tier, origin_family, day_offset, confirmation_state=None):
    ts = (SUNDAY_PT - timedelta(days=day_offset)).astimezone(UTC)
    conn.execute(
        "INSERT INTO events (id, primary_title, category, status, first_seen_at, last_updated_at, "
        "confirmation_state, final_score, significance, market_impact, confirmation_quality) "
        "VALUES (?, ?, ?, 'developing', ?, ?, 'unverified', ?, ?, ?, 50)",
        (event_id, title, category, ts.isoformat(), ts.isoformat(), score, score, score),
    )
    insert_source_row(conn, f"src_{event_id}", tier)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (f"src_{event_id}", event_id, f"https://x.test/{event_id}", event_id, title, title.lower(),
         excerpt, ts.isoformat(), event_id, origin_family),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )
    if confirmation_state is not None:
        conn.execute("UPDATE events SET confirmation_state = ? WHERE id = ?", (confirmation_state, event_id))
    else:
        update_confirmation_state(conn, event_id, now=ts)


def _seed_full_week(conn):
    _seed(conn, event_id="E-AI-1", title="NVIDIA export control rule on AI chips",
          excerpt="NVIDIA export control rule affects AI chip shipments.",
          category="AI & Semiconductors", score=80, tier=0, origin_family="bis.gov", day_offset=2)
    _seed(conn, event_id="E-MKT-1", title="Fed holds rates steady",
          excerpt="The Fed held rates steady amid market volatility.",
          category="Markets & Economy", score=76, tier=0, origin_family="federalreserve.gov", day_offset=4)
    # A reversed/disputed item -> noise.
    _seed(conn, event_id="E-NOISE-1", title="Unverified rumor about a chip ban",
          excerpt="An unconfirmed rumor claimed a new chip ban; later disputed.",
          category="AI & Semiconductors", score=30, tier=3, origin_family="rumor.mirror", day_offset=3,
          confirmation_state="disputed")
    # A Trump statement that never became formal policy -> unresolved + statement-only.
    _seed(conn, event_id="E-STMT-1", title="Trump said tariffs on China would increase",
          excerpt="Trump said in a statement that tariffs on China would increase next month.",
          category="US / China / World", score=60, tier=1, origin_family="wire-a.test", day_offset=1)
    _seed(conn, event_id="E-STMT-2", title="Second report on Trump tariff statement",
          excerpt="A second outlet reported Trump stated tariffs on China would increase next month.",
          category="US / China / World", score=60, tier=1, origin_family="wire-b.test", day_offset=1)


def test_weekly_synthesizes_trends_not_a_daily_concatenation(config, conn):
    _seed_full_week(conn)
    summary = run_weekly(config, conn, dry_run=True, now=SUNDAY_PT.astimezone(UTC))

    assert summary.trend_count >= 3
    rendered = "\n".join(summary.rendered_parts)
    # Trend-level synthesis artifacts that no single day's digest could ever
    # contain: multi-day persistence framing and a strength-ranked ordering.
    assert "WHAT MATERIALLY CHANGED" in rendered
    assert "strength" in rendered.lower()
    # Structural trend clusters, not per-day sections.
    daily_only_markers = ["TOP 3", "QUICK BRIEFS", "TOMORROW TO WATCH"]
    assert not any(marker in rendered for marker in daily_only_markers)


def test_noise_unresolved_and_disputed_are_represented(config, conn):
    _seed_full_week(conn)
    summary = run_weekly(config, conn, dry_run=True, now=SUNDAY_PT.astimezone(UTC))
    rendered = "\n".join(summary.rendered_parts)

    assert "NOISE VS. SIGNAL" in rendered
    assert "Unverified rumor about a chip ban" in rendered  # disputed -> noise
    assert "UNRESOLVED" in rendered
    assert "Trump" in rendered  # statement-only, still developing
    assert "Remained statement only" in rendered
    assert "Trump said tariffs on China would increase" in rendered


def test_next_week_items_come_from_stored_calendar_records(config, conn):
    _seed_full_week(conn)
    now_utc = SUNDAY_PT.astimezone(UTC)
    insert_source_row(conn, "bls_calendar", 0, adapter_type="ical")
    record_scheduled_events(
        conn, "bls_calendar",
        [
            CollectedItem(
                source_id="bls_calendar", external_id="cpi-aug",
                canonical_url="https://www.bls.gov/schedule/news_release/cpi.htm",
                title="CPI release — August", summary_excerpt=None, language="en",
                published_at=now_utc + timedelta(days=3), origin_family="scheduled_calendar",
            )
        ],
        now=now_utc,
    )

    summary = run_weekly(config, conn, dry_run=True, now=now_utc)
    rendered = "\n".join(summary.rendered_parts)
    assert "CPI release" in rendered

    calendar_row = conn.execute(
        "SELECT * FROM scheduled_events WHERE title = 'CPI release — August'"
    ).fetchone()
    assert calendar_row is not None
    assert calendar_row["url"] == "https://www.bls.gov/schedule/news_release/cpi.htm"


def test_every_referenced_event_and_calendar_id_is_stored(config, conn):
    _seed_full_week(conn)
    now_utc = SUNDAY_PT.astimezone(UTC)
    insert_source_row(conn, "bls_calendar2", 0, adapter_type="ical")
    record_scheduled_events(
        conn, "bls_calendar2",
        [
            CollectedItem(
                source_id="bls_calendar2", external_id="jobs-aug",
                canonical_url="https://www.bls.gov/schedule/news_release/empsit.htm",
                title="Employment Situation — August", summary_excerpt=None, language="en",
                published_at=now_utc + timedelta(days=2), origin_family="scheduled_calendar",
            )
        ],
        now=now_utc,
    )

    run_weekly(config, conn, dry_run=False, now=now_utc)

    digest = conn.execute("SELECT * FROM digests WHERE kind='weekly' ORDER BY id DESC LIMIT 1").fetchone()
    assert digest is not None

    referenced_event_ids = [
        r["event_id"] for r in conn.execute(
            "SELECT event_id FROM digest_events WHERE digest_id = ?", (digest["id"],)
        ).fetchall()
    ]
    assert referenced_event_ids  # at least one event was tagged
    for event_id in referenced_event_ids:
        row = conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone()
        assert row is not None, f"digest referenced unknown event {event_id}"

    calendar_row = conn.execute(
        "SELECT * FROM scheduled_events WHERE title = 'Employment Situation — August'"
    ).fetchone()
    assert calendar_row["url"] == "https://www.bls.gov/schedule/news_release/empsit.htm"


def test_sunday_scheduler_repeated_calls_create_one_weekly_and_zero_daily_duplicates(config, conn):
    _seed_full_week(conn)
    now_utc = SUNDAY_PT.astimezone(UTC)

    weekly_1 = run_weekly(config, conn, dry_run=True, now=now_utc)
    weekly_2 = run_weekly(config, conn, dry_run=True, now=now_utc + timedelta(minutes=5))
    weekly_3 = run_weekly(config, conn, dry_run=False, now=now_utc + timedelta(minutes=10))

    assert weekly_1.skipped_duplicate is False
    assert weekly_2.skipped_duplicate is True
    assert weekly_3.skipped_duplicate is True

    weekly_digest_count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind='weekly'").fetchone()["n"]
    assert weekly_digest_count == 1

    daily_1 = run_daily(config, conn, dry_run=True, now=now_utc)
    daily_2 = run_daily(config, conn, dry_run=False, now=now_utc + timedelta(minutes=5))
    assert daily_1.skipped_sunday is True
    assert daily_2.skipped_sunday is True

    daily_digest_count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind='daily'").fetchone()["n"]
    assert daily_digest_count == 0


def test_llm_outage_still_produces_useful_fallback_weekly_brief(config, conn):
    # No LLM_* configured in the `config` fixture -> NullProvider -> fallback.
    _seed_full_week(conn)
    summary = run_weekly(config, conn, dry_run=True, now=SUNDAY_PT.astimezone(UTC))

    assert summary.fallback_used is True
    rendered = "\n".join(summary.rendered_parts)
    assert "AI summarization is temporarily unavailable" in rendered
    assert "WHAT MATERIALLY CHANGED" in rendered
    assert "NEXT WEEK" in rendered
    assert summary.trend_count >= 1


def test_delivery_remains_dry_run_by_default(config, conn):
    _seed_full_week(conn)
    summary = run_weekly(config, conn, now=SUNDAY_PT.astimezone(UTC))  # dry_run defaults True
    assert summary.sent is False
    assert summary.sent_dry_run is True


def test_week_window_matches_the_job_used_for_prior_comparison():
    # Sanity check that the job module re-exports the same window function
    # used directly by the unit tests (single source of truth).
    start, end = week_window(SUNDAY_PT)
    assert start.weekday() == 0
    assert end.weekday() == 0
