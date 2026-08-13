"""2026-08-13 reconciliation: proves the cross-day novelty lookback cannot
be contaminated by the very digest a replay/audit is trying to reconstruct
the state before. event_repo.recent_digest_events()'s new `until` bound
(set to this run's own window_start in jobs/daily.py) structurally excludes
any digest whose window overlaps or follows the one being computed --
including, critically, a re-query issued *after* that same digest has
already been recorded (the exact situation a post-hoc replay is in)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.event_repo import recent_digest_events
from news_intel.jobs.daily import run_daily
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from tests.conftest import insert_source_row


def _seed_event(conn, *, event_id, title, excerpt, now, source_id="nvidia_newsroom"):
    insert_source_row(conn, source_id, 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, 'unverified')",
        (event_id, title, now.isoformat(), now.isoformat(), now.isoformat()),
    )
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, 'nvidia.com')",
        (source_id, event_id, f"https://nvidia.com/{event_id}", event_id, title, title.lower(), excerpt,
         now.isoformat(), event_id),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )
    update_confirmation_state(conn, event_id)


def _score(conn, config, event_id, now):
    scores = score_event(conn, event_id, config, is_new_event=True, now=now)
    weights = config.policy.scoring.weights.model_dump()
    persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)


def test_target_date_own_digest_cannot_contaminate_its_own_novelty_check(config, conn):
    """Deliver a real digest for a day, then reconstruct "what was prior
    coverage as of that same day's own selection" -- the day's own
    just-created digest must never appear in that reconstruction."""
    day1 = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-DAY1",
        title="NVIDIA announces new AI compute product for developers",
        excerpt="NVIDIA today announced a new AI compute product aimed at enterprise developers.",
        now=day1,
    )
    _score(conn, config, "E-DAY1", day1)
    summary = run_daily(config, conn, dry_run=True, now=day1)
    assert summary.top3_count + summary.quick_brief_count >= 1  # sanity: something was actually delivered

    window_start_as_of_day1 = day1 - timedelta(hours=24)  # matches what _select_events used internally
    since = window_start_as_of_day1 - timedelta(days=8)

    # WITHOUT the `until` bound: the day's own already-recorded digest
    # leaks in as "prior coverage" -- this is the self-contamination bug.
    unbounded = recent_digest_events(conn, since=since, kind="daily")
    assert "E-DAY1" in [p["event_id"] for p in unbounded]

    # WITH `until` set to that same day's own window_start (exactly what
    # jobs/daily.py now always passes): the day's own digest is excluded.
    bounded = recent_digest_events(conn, since=since, until=window_start_as_of_day1, kind="daily")
    assert "E-DAY1" not in [p["event_id"] for p in bounded]


def test_recent_digest_events_until_excludes_digests_ending_after_the_bound(config, conn):
    """Direct unit-level proof of the `until` semantics: a digest whose
    window_end is strictly after `until` is excluded regardless of how far
    in the past its window_start was."""
    day1 = datetime(2026, 8, 10, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-EARLY", title="NVIDIA ships an early release",
        excerpt="NVIDIA shipped an early release of its developer toolkit today.", now=day1,
    )
    _score(conn, config, "E-EARLY", day1)
    run_daily(config, conn, dry_run=True, now=day1)

    cutoff = day1 - timedelta(hours=1)  # strictly before this digest's own window_end
    results = recent_digest_events(conn, since=day1 - timedelta(days=8), until=cutoff, kind="daily")
    assert "E-EARLY" not in [p["event_id"] for p in results]
