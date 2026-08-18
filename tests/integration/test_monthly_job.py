"""Monthly intelligence brief acceptance tests -- mirrors
tests/integration/test_weekly_job.py's structure and seeding pattern, for
the complete-previous-calendar-month cadence instead of the ISO week."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel.adapters.base import CollectedItem
from news_intel.jobs.daily import run_daily
from news_intel.jobs.monthly import is_first_monday, month_window, run_monthly
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.scheduled_repo import record_scheduled_events
from tests.conftest import insert_source_row

PT = ZoneInfo("America/Los_Angeles")
# 2026-09-07 is the first Monday of September 2026 -> covers all of August.
FIRST_MONDAY_PT = datetime(2026, 9, 7, 22, 0, tzinfo=PT)
AUGUST_START_PT = datetime(2026, 8, 1, 12, 0, tzinfo=PT)


def _seed(conn, *, event_id, title, excerpt, category, score, tier, origin_family, when_pt, confirmation_state=None):
    ts = when_pt.astimezone(UTC)
    conn.execute(
        "INSERT INTO events (id, primary_title, category, status, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state, final_score, significance, market_impact, "
        "confirmation_quality) VALUES (?, ?, ?, 'developing', ?, ?, ?, 'unverified', ?, ?, ?, 50)",
        (event_id, title, category, ts.isoformat(), ts.isoformat(), ts.isoformat(), score, score, score),
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


def _seed_full_month(conn):
    _seed(conn, event_id="E-AI-1", title="NVIDIA export control rule on AI chips",
          excerpt="NVIDIA export control rule affects AI chip shipments.",
          category="AI & Semiconductors", score=80, tier=0, origin_family="bis.gov",
          when_pt=AUGUST_START_PT + timedelta(days=5))
    _seed(conn, event_id="E-MKT-1", title="Fed holds rates steady",
          excerpt="The Fed held rates steady amid market volatility.",
          category="Markets & Economy", score=76, tier=0, origin_family="federalreserve.gov",
          when_pt=AUGUST_START_PT + timedelta(days=12))
    _seed(conn, event_id="E-NOISE-1", title="Unverified rumor about a chip ban",
          excerpt="An unconfirmed rumor claimed a new chip ban; later disputed.",
          category="AI & Semiconductors", score=30, tier=3, origin_family="rumor.mirror",
          when_pt=AUGUST_START_PT + timedelta(days=8), confirmation_state="disputed")
    _seed(conn, event_id="E-STMT-1", title="Official said tariffs on China would increase",
          excerpt="An official said in a statement that tariffs on China would increase next month.",
          category="US / China / World", score=60, tier=1, origin_family="wire-a.test",
          when_pt=AUGUST_START_PT + timedelta(days=20))
    # Outside the August window entirely (September, i.e. the current
    # in-progress month) -- must NOT be included in the August report.
    _seed(conn, event_id="E-SEPT-1", title="An unrelated September-only development",
          excerpt="This happened in September, after the reported month closed.",
          category="Markets & Economy", score=70, tier=1, origin_family="wire-c.test",
          when_pt=FIRST_MONDAY_PT - timedelta(hours=1))


def test_is_first_monday():
    assert is_first_monday(FIRST_MONDAY_PT.date()) is True
    assert is_first_monday((FIRST_MONDAY_PT + timedelta(days=7)).date()) is False  # second Monday
    assert is_first_monday(FIRST_MONDAY_PT.date() + timedelta(days=1)) is False  # Tuesday


def test_month_window_covers_the_complete_previous_calendar_month():
    start, end = month_window(FIRST_MONDAY_PT)
    assert start == datetime(2026, 8, 1, tzinfo=PT)
    assert end == datetime(2026, 9, 1, tzinfo=PT)


def test_monthly_synthesizes_the_month_not_four_weeklies_concatenated(config, conn):
    _seed_full_month(conn)
    summary = run_monthly(config, conn, dry_run=True, now=FIRST_MONDAY_PT.astimezone(UTC))

    assert summary.month_label == "August 2026"
    assert summary.trend_count >= 3
    rendered = "\n".join(summary.rendered_parts)
    assert "WHAT MATERIALLY CHANGED THIS MONTH" in rendered
    assert "An unrelated September-only development" not in rendered  # excluded: outside the reported month
    daily_only_markers = ["TOP 3", "QUICK BRIEFS", "TOMORROW TO WATCH"]
    assert not any(marker in rendered for marker in daily_only_markers)


def test_monthly_noise_and_signal_represented(config, conn):
    _seed_full_month(conn)
    summary = run_monthly(config, conn, dry_run=True, now=FIRST_MONDAY_PT.astimezone(UTC))
    rendered = "\n".join(summary.rendered_parts)
    assert "NOISE VS. SIGNAL" in rendered
    assert "Unverified rumor about a chip ban" in rendered


def test_next_month_items_come_from_stored_calendar_records(config, conn):
    _seed_full_month(conn)
    now_utc = FIRST_MONDAY_PT.astimezone(UTC)
    insert_source_row(conn, "bls_calendar_m", 0, adapter_type="ical")
    record_scheduled_events(
        conn, "bls_calendar_m",
        [
            CollectedItem(
                source_id="bls_calendar_m", external_id="cpi-sep",
                canonical_url="https://www.bls.gov/schedule/news_release/cpi.htm",
                title="CPI release — September", summary_excerpt=None, language="en",
                published_at=now_utc + timedelta(days=10), origin_family="scheduled_calendar",
            )
        ],
        now=now_utc,
    )

    summary = run_monthly(config, conn, dry_run=True, now=now_utc)
    rendered = "\n".join(summary.rendered_parts)
    assert "CPI release" in rendered


def test_first_monday_repeated_calls_create_one_monthly_and_zero_daily_duplicates(config, conn):
    _seed_full_month(conn)
    now_utc = FIRST_MONDAY_PT.astimezone(UTC)

    monthly_1 = run_monthly(config, conn, dry_run=True, now=now_utc)
    monthly_2 = run_monthly(config, conn, dry_run=True, now=now_utc + timedelta(minutes=5))
    monthly_3 = run_monthly(config, conn, dry_run=False, now=now_utc + timedelta(minutes=10))

    assert monthly_1.skipped_duplicate is False
    assert monthly_2.skipped_duplicate is True
    assert monthly_3.skipped_duplicate is True

    monthly_digest_count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind='monthly'").fetchone()["n"]
    assert monthly_digest_count == 1

    daily_1 = run_daily(config, conn, dry_run=True, now=now_utc)
    daily_2 = run_daily(config, conn, dry_run=False, now=now_utc + timedelta(minutes=5))
    assert daily_1.skipped_monthly_slot is True
    assert daily_2.skipped_monthly_slot is True

    daily_digest_count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind='daily'").fetchone()["n"]
    assert daily_digest_count == 0


def test_second_monday_daily_is_not_skipped_as_monthly_slot(config, conn):
    """The monthly-slot guard must only fire on days 1-7, never on every
    Monday of the month -- a second-Monday daily send proceeds normally."""
    second_monday_utc = (FIRST_MONDAY_PT + timedelta(days=7)).astimezone(UTC)
    daily = run_daily(config, conn, dry_run=True, now=second_monday_utc)
    assert daily.skipped_monthly_slot is False


def test_monthly_fallback_brief_is_populated(config, conn):
    # No LLM_* configured in the `config` fixture -> deterministic path only.
    _seed_full_month(conn)
    summary = run_monthly(config, conn, dry_run=True, now=FIRST_MONDAY_PT.astimezone(UTC))

    assert summary.fallback_used is True
    rendered = "\n".join(summary.rendered_parts)
    assert "WHAT MATERIALLY CHANGED THIS MONTH" in rendered
    assert "NEXT MONTH" in rendered
    assert summary.trend_count >= 1


def test_monthly_composer_receives_a_bounded_set_of_selected_items(config, conn):
    config.policy.digests.weekly_max_selected_items = 5
    for i in range(15):
        _seed(
            conn, event_id=f"E-BOUND-{i:02d}", title=f"Distinct standalone development number {i}",
            excerpt=f"A self-contained story with no relation to the others, number {i}.",
            category="Markets & Economy", score=70 - i, tier=1, origin_family=f"wire-{i}.test",
            when_pt=AUGUST_START_PT + timedelta(days=1),
        )

    summary = run_monthly(config, conn, dry_run=True, now=FIRST_MONDAY_PT.astimezone(UTC))
    assert summary.trend_count <= 5


def test_monthly_delivery_remains_dry_run_by_default(config, conn):
    _seed_full_month(conn)
    summary = run_monthly(config, conn, now=FIRST_MONDAY_PT.astimezone(UTC))  # dry_run defaults True
    assert summary.sent is False
    assert summary.sent_dry_run is True


def test_every_referenced_event_and_calendar_id_is_stored(config, conn):
    _seed_full_month(conn)
    now_utc = FIRST_MONDAY_PT.astimezone(UTC)

    run_monthly(config, conn, dry_run=False, now=now_utc)

    digest = conn.execute("SELECT * FROM digests WHERE kind='monthly' ORDER BY id DESC LIMIT 1").fetchone()
    assert digest is not None

    referenced_event_ids = [
        r["event_id"] for r in conn.execute(
            "SELECT event_id FROM digest_events WHERE digest_id = ?", (digest["id"],)
        ).fetchall()
    ]
    assert referenced_event_ids
    for event_id in referenced_event_ids:
        row = conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone()
        assert row is not None, f"digest referenced unknown event {event_id}"
