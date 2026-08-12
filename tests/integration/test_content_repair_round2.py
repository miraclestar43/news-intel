"""2026-08-12 content-quality repair, round 2: cross-day novelty suppression,
publisher-family normalization end-to-end, GDELT/original-publisher
provenance, and TOMORROW TO WATCH human-readable-link/next-day filtering --
reproducing the exact defects observed in the 2026-08-11 production digest
(NVIDIA financing recap re-selected as new, blogs.nvidia.com +
developer.nvidia.com both in TOP, PPI two days out shown as tomorrow, all
three calendar links pointing at the raw .ics feed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.adapters.base import CollectedItem
from news_intel.event_repo import record_digest
from news_intel.jobs.daily import run_daily
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.scheduled_repo import record_scheduled_events
from tests.conftest import insert_source_row


def _seed_event(
    conn, *, event_id, title, excerpt, category, now, source_id, tier=0,
    origin_family="example.test", source_name=None, canonical_url=None,
):
    insert_source_row(conn, source_id, tier, name=source_name)
    conn.execute(
        "INSERT INTO events (id, primary_title, category, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, ?, 'unverified')",
        (event_id, title, category, now.isoformat(), now.isoformat(), now.isoformat()),
    )
    url = canonical_url or f"https://{origin_family}/{event_id}"
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (source_id, event_id, url, event_id, title, title.lower(), excerpt, now.isoformat(), event_id, origin_family),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )
    return raw_id


def _score(conn, config, event_id, now):
    update_confirmation_state(conn, event_id)
    scores = score_event(conn, event_id, config, is_new_event=True, now=now)
    weights = config.policy.scoring.weights.model_dump()
    persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)


# --- cross-day novelty --------------------------------------------------


def test_pure_recap_of_yesterdays_top_story_is_suppressed(config, conn):
    """Exact 2026-08-10 -> 2026-08-11 reproduction: a financing announcement
    delivered yesterday, restated under a new headline/URL today with no
    new fact, must not be re-selected."""
    day1 = datetime(2026, 8, 10, 22, 0, tzinfo=UTC)
    day2 = day1 + timedelta(days=1)

    _seed_event(
        conn, event_id="E-DAY1", category="AI & Semiconductors", now=day1, source_id="nvidia_newsroom",
        title="NVIDIA Partners With Apollo and KKR to Mobilize Over $500 Billion of Capital",
        excerpt="NVIDIA today announced financing platforms with Apollo, BlackRock and KKR to mobilize $500 billion.",
        origin_family="nvidia.com",
    )
    _score(conn, config, "E-DAY1", day1)
    record_digest(
        conn, kind="daily", window_start=day1 - timedelta(hours=24), window_end=day1,
        rendered_text="prior digest", model_used="none", fallback_used=True,
        event_ids_top3=["E-DAY1"], event_ids_quick=[],
    )

    _seed_event(
        conn, event_id="E-DAY2-RECAP", category="AI & Semiconductors", now=day2, source_id="nvidia_newsroom",
        title="NVIDIA AI Factory Compute Is Becoming an Investable Asset Class",
        excerpt=(
            "We announced partnerships with Apollo, BlackRock and KKR to mobilize over $500 billion of "
            "third-party capital for AI infrastructure."
        ),
        origin_family="nvidia.com",
    )
    _score(conn, config, "E-DAY2-RECAP", day2)

    summary = run_daily(config, conn, dry_run=True, now=day2)
    assert "E-DAY2-RECAP" not in summary.rendered_text
    assert "Investable Asset Class" not in summary.rendered_text


def test_genuine_material_update_is_shown_with_update_note(config, conn):
    """The SAME story, but the new telling adds a concrete new figure and
    status-change language -- must be selected, with a rendered note
    stating what changed."""
    day1 = datetime(2026, 8, 10, 22, 0, tzinfo=UTC)
    day2 = day1 + timedelta(days=1)

    _seed_event(
        conn, event_id="E-DEAL-TALK", category="Markets & Economy", now=day1, source_id="fed_wire",
        title="Federal Reserve reviews proposed bank merger amid banking stress",
        excerpt="The Federal Reserve is reportedly reviewing a proposed $2 billion bank merger amid banking stress.",
        origin_family="fed.example",
    )
    _score(conn, config, "E-DEAL-TALK", day1)
    record_digest(
        conn, kind="daily", window_start=day1 - timedelta(hours=24), window_end=day1,
        rendered_text="prior digest", model_used="none", fallback_used=True,
        event_ids_top3=["E-DEAL-TALK"], event_ids_quick=[],
    )

    _seed_event(
        conn, event_id="E-DEAL-APPROVED", category="Markets & Economy", now=day2, source_id="fed_wire",
        title="Federal Reserve approves bank merger at higher valuation",
        excerpt=(
            "The Federal Reserve has approved the bank merger amid banking stress, now valued at $3 billion, "
            "finalizing the deal."
        ),
        origin_family="fed.example",
    )
    _score(conn, config, "E-DEAL-APPROVED", day2)

    summary = run_daily(config, conn, dry_run=True, now=day2)
    assert "E-DEAL-APPROVED" in summary.rendered_text or "higher valuation" in summary.rendered_text
    assert "Update since prior coverage" in summary.rendered_text


# --- publisher-family normalization end-to-end ---------------------------


def test_nvidia_subdomains_normalize_to_one_publisher_family_end_to_end(config, conn):
    """2026-08-11 defect reproduction: blogs.nvidia.com and
    developer.nvidia.com must resolve to the same publisher-family key
    through the real stored-URL path (event_repo -> registrable_domain),
    not just in isolated unit fixtures. The swap-enforcement behavior this
    feeds is covered separately in
    tests/unit/test_daily_selection_and_formatting.py::test_at_most_one_domain_in_top_stories."""
    now = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-BLOG", category="AI & Semiconductors", now=now, source_id="nvidia_newsroom",
        title="NVIDIA AI Factory Compute Is Becoming an Investable Asset Class",
        excerpt="A distinct announcement about AI factory financing structures for enterprise buyers everywhere.",
        canonical_url="https://blogs.nvidia.com/blog/ai-factory-compute",
    )
    _score(conn, config, "E-BLOG", now)
    _seed_event(
        conn, event_id="E-DEV", category="AI & Semiconductors", now=now, source_id="nvidia_dev_blog",
        title="NVIDIA JetPack 7.2.1 Adds Agentic Video Skills",
        excerpt="A distinct developer-tooling release note about JetPack video pipeline capabilities.",
        canonical_url="https://developer.nvidia.com/blog/jetpack-7-2-1",
    )
    _score(conn, config, "E-DEV", now)

    from news_intel.jobs.daily import _select_events

    _, result = _select_events(conn, config, now)
    domains = {s.event_id: s.domain for s in result.all_stories}
    assert domains["E-BLOG"] == "nvidia.com"
    assert domains["E-DEV"] == "nvidia.com"


# --- GDELT vs original-publisher provenance -------------------------------


def test_gdelt_discovered_item_displays_original_publisher_not_gdelt(config, conn):
    now = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-GDELT", category="Markets & Economy", now=now, source_id="gdelt_doc", tier=3,
        source_name="GDELT DOC API", origin_family="zerohedge.com",
        title="Regulators announce new sanctions amid market volatility",
        excerpt="Regulators announced new sanctions today amid broad market volatility and selloff pressure.",
        canonical_url="https://zerohedge.com/markets/sanctions-story",
    )
    _score(conn, config, "E-GDELT", now)

    summary = run_daily(config, conn, dry_run=True, now=now)
    if "E-GDELT" in [line for line in summary.rendered_text.splitlines()]:
        pass  # presence isn't asserted here -- selection depends on score; provenance is checked directly below
    from news_intel.event_repo import event_sources

    sources = event_sources(conn, "E-GDELT")
    assert sources[0]["publisher"] == "zerohedge.com"
    assert "GDELT" not in sources[0]["publisher"]


def test_techmeme_discovered_item_keeps_its_own_publisher_name(config, conn):
    """Techmeme's canonical_url points at Techmeme's own roundup page, not
    the original article -- unlike GDELT, its source name must NOT be
    replaced by origin_family (that would misattribute Techmeme's own
    aggregation page as if it were the original publisher)."""
    now = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-TECHMEME", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", origin_family="www.techmeme.com",
        title="Aggregated coverage of a major AI story",
        excerpt="Techmeme links to a Financial Times report on a major AI industry story today.",
        canonical_url="https://www.techmeme.com/260811/p1",
    )
    _score(conn, config, "E-TECHMEME", now)
    from news_intel.event_repo import event_sources

    sources = event_sources(conn, "E-TECHMEME")
    assert sources[0]["publisher"] == "Techmeme"


# --- qualification: human-readable URL ------------------------------------


def test_candidate_with_only_machine_feed_url_is_excluded(config, conn):
    now = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-FEED-ONLY", category="Markets & Economy", now=now, source_id="feed_src",
        title="Federal Reserve announces major policy action amid banking stress",
        excerpt="The Federal Reserve announced a major policy action today amid ongoing banking stress.",
        canonical_url="https://example.gov/feed.xml",
    )
    _score(conn, config, "E-FEED-ONLY", now)
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "E-FEED-ONLY" not in summary.rendered_text
    assert "Federal Reserve announces major policy action" not in summary.rendered_text


# --- TOMORROW TO WATCH -----------------------------------------------------


def _seed_bls_calendar(conn, now):
    """`now` = 2026-08-11 22:00 UTC = 2026-08-11 15:00 PDT (Pacific digest
    date Aug 11; "tomorrow" is Aug 12 Pacific). CPI is scheduled for Aug 12
    08:30 PDT = Aug 12 15:30 UTC (tomorrow -- must be shown); PPI for Aug 13
    08:30 PDT = Aug 13 15:30 UTC (two Pacific days out -- must be excluded)."""
    insert_source_row(conn, "bls_calendar", 0, adapter_type="ical")
    record_scheduled_events(
        conn, "bls_calendar",
        [
            CollectedItem(
                source_id="bls_calendar", external_id="cpi", canonical_url="https://www.bls.gov/schedule/news_release/bls.ics",
                title="Consumer Price Index", summary_excerpt=None, language="en",
                published_at=datetime(2026, 8, 12, 15, 30, tzinfo=UTC), origin_family="scheduled_calendar",
            ),
            CollectedItem(
                source_id="bls_calendar", external_id="ppi", canonical_url="https://www.bls.gov/schedule/news_release/bls.ics",
                title="Producer Price Index", summary_excerpt=None, language="en",
                published_at=datetime(2026, 8, 13, 15, 30, tzinfo=UTC), origin_family="scheduled_calendar",
            ),
        ],
    )


def test_tomorrow_to_watch_shows_human_readable_cpi_link_and_excludes_two_day_out_ppi(config, conn):
    now = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)  # Pacific digest date Aug 11; "tomorrow" = Aug 12
    _seed_bls_calendar(conn, now)
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "https://www.bls.gov/schedule/news_release/cpi.htm" in summary.rendered_text
    assert "Producer Price Index" not in summary.rendered_text  # two Pacific days out, not tomorrow


def test_tomorrow_to_watch_never_displays_an_ics_link(config, conn):
    now = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)
    _seed_bls_calendar(conn, now)
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert ".ics" not in summary.rendered_text
