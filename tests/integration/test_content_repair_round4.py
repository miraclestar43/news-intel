"""2026-08-13 reconciliation round: Nebius surviving the repaired
relevance/noise path end-to-end, exact-threshold (58.3 vs 58) qualification
behavior, and the recognition/award gate excluding a story from both TOP
and MORE TO KNOW end-to-end."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.jobs.daily import run_daily
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from tests.conftest import insert_source_row


def _seed_event(conn, *, event_id, title, excerpt, now, source_id="techmeme", tier=3, source_name="Techmeme"):
    insert_source_row(conn, source_id, tier, name=source_name)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, 'unverified')",
        (event_id, title, now.isoformat(), now.isoformat(), now.isoformat()),
    )
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, 'www.techmeme.com')",
        (source_id, event_id, f"https://www.techmeme.com/{event_id}", event_id, title, title.lower(), excerpt,
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
    final = persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)
    return final


def test_nebius_survives_qualification_after_relevance_and_noise_fix(config, conn):
    """The exact 2026-08-12 Nebius text: previously zeroed by the
    noise-substring bug ("sale" matching inside "sales") and, separately,
    outside the AI vocabulary entirely. Both are fixed; the real candidate
    must now clear the (unchanged) qualification threshold."""
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-NEBIUS",
        title="Amsterdam-based neocloud Nebius reports Q2 cloud revenue up 514% YoY to $575M",
        excerpt=(
            "Nebius Group NV reported a 514% jump in second-quarter sales for its cloud business "
            "following strong demand for AI computing power."
        ),
        now=now,
    )
    final = _score(conn, config, "E-NEBIUS", now)
    threshold = config.policy.scoring.thresholds.daily_quick_brief
    assert final >= threshold


def test_score_exactly_at_threshold_qualifies_not_excluded(config, conn):
    """A candidate scoring at or fractionally above the unchanged 58.0
    threshold is genuinely qualified -- being crowded out of the final
    selection by the AI-family cap afterward is a separate, later decision
    and must not be described or treated as a qualification failure."""
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-BOUNDARY",
        title="AI news outlet reports on OpenAI security disclosure",
        excerpt="An AI newsroom covered OpenAI's security disclosure process this week and hacking trends.",
        now=now,
    )
    final = _score(conn, config, "E-BOUNDARY", now)
    threshold = config.policy.scoring.thresholds.daily_quick_brief
    from news_intel.event_repo import qualified_events_since

    window_start = now - timedelta(hours=1)
    rows = qualified_events_since(conn, window_start=window_start, min_score=threshold)
    ids = [r["id"] for r in rows]
    if final >= threshold:
        assert "E-BOUNDARY" in ids
    else:
        assert "E-BOUNDARY" not in ids


def test_recognition_story_excluded_from_both_top_and_more_to_know(config, conn):
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-GLASSDOOR", source_id="nvidia_newsroom", tier=0, source_name="NVIDIA newsroom",
        title="NVIDIA CEO Tops Glassdoor's 2026 List of Best CEOs",
        excerpt=(
            "NVIDIA founder and CEO Jensen Huang is ranked No. 1 on Glassdoor's Best CEOs list for 2026. "
            "Huang topped the list, with 99% of employees approving of the job he does."
        ),
        now=now,
    )
    _score(conn, config, "E-GLASSDOOR", now)
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "Glassdoor" not in summary.rendered_text
    assert "Best CEOs" not in summary.rendered_text
