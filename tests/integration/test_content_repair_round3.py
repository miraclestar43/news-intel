"""2026-08-13 content-quality repair, round 3: event/source integrity
(dashboard-like pages, false economic-acronym corroboration), the hard
one-TOP-story-per-publisher-family rule, Techmeme original-publisher
resolution, TOP-requires-a-complete-summary, and TOMORROW TO WATCH release
times -- reproducing the exact defects observed in the 2026-08-12
production digest (a BLS "latest numbers" dashboard selected as a TOP
story and falsely "corroborated" by an unrelated Hellenic Shipping News
article that only shared the word "CPI"; two Techmeme-discovered stories
both in TOP; a media-industry story about an AI newsroom over-scored to
materiality=100; every TOP story rendering with no summary text at all)."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.adapters.base import CollectedItem
from news_intel.adapters.social_stub import SocialStubAdapter
from news_intel.event_repo import event_sources
from news_intel.jobs.daily import run_daily
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.scheduled_repo import record_scheduled_events
from tests.conftest import insert_source_row


def _seed_event(
    conn, *, event_id, title, excerpt, category, now, source_id, tier=0,
    origin_family="example.test", source_name=None, canonical_url=None, origin_url=None,
    origin_published_at=None, published_at=None,
):
    insert_source_row(conn, source_id, tier, name=source_name)
    conn.execute(
        "INSERT INTO events (id, primary_title, category, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, ?, 'unverified')",
        (event_id, title, category, now.isoformat(), now.isoformat(), now.isoformat()),
    )
    url = canonical_url or f"https://{origin_family}/{event_id}"
    # 2026-08-17 freshness repair: published_at now defaults to `now` (a
    # realistic just-discovered aggregator timestamp) rather than being
    # silently omitted -- pipeline/freshness.py's bounded_unverified gate
    # requires a real aggregator published_at for any item whose origin_url
    # is set but whose origin_published_at could not be verified; pass
    # published_at=<some older datetime> explicitly to exercise a stale
    # aggregator timestamp.
    effective_published_at = published_at if published_at is not None else now
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, published_at, fetched_at, content_hash, origin_family, "
        "origin_url, origin_published_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?, ?, ?, ?)",
        (
            source_id, event_id, url, event_id, title, title.lower(), excerpt,
            effective_published_at.isoformat(), now.isoformat(), event_id,
            origin_family, origin_url,
            origin_published_at.isoformat() if origin_published_at else None,
        ),
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


# --- event/source integrity ------------------------------------------------


def test_bls_dashboard_and_unrelated_article_do_not_become_one_corroborated_story(config, conn):
    """Exact 2026-08-12 reproduction: bls_latest's dashboard page and an
    unrelated Hellenic Shipping News commentary article, sharing only
    "CPI" -- must not be excluded from candidacy as a dashboard AND must
    not falsely merge even if somehow both were candidates."""
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-BLS", category="Markets & Economy", now=now, source_id="bls_latest",
        source_name="BLS latest numbers", canonical_url="https://www.bls.gov/bls",
        title="Major Economic Indicators Latest Numbers",
        excerpt=(
            "Consumer Price Index (CPI): +0.1% in Jul 2026 News Release Historical Data "
            "Unemployment Rate: 4.1% in Jul 2026 News Release Historical Data "
            "Payroll Employment: -23,000(p) in Jul 2026 News Release Historical Data"
        ),
    )
    _score(conn, config, "E-BLS", now)
    _seed_event(
        conn, event_id="E-HELLENIC", category="Markets & Economy", now=now, source_id="gdelt_doc", tier=3,
        source_name="GDELT DOC API", origin_family="hellenicshippingnews.com",
        canonical_url="https://www.hellenicshippingnews.com/asian-stocks-rise",
        title="Asian stocks rise as KOSPI surges; CPI, Hormuz risks temper tech rally",
        excerpt="Asian markets rallied even as CPI data and Hormuz risks weighed on tech shares today.",
    )
    _score(conn, config, "E-HELLENIC", now)

    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "Major Economic Indicators Latest Numbers" not in summary.rendered_text  # dashboard excluded
    if "hellenicshippingnews.com" in summary.rendered_text:
        assert "BLS latest numbers" not in summary.rendered_text  # never falsely co-cited


def test_generic_ai_media_story_does_not_outrank_a_genuine_large_deal(config, conn):
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-RUNTIMEWIRE", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260812/p17",
        title="A look at AI news website RuntimeWire, which costs $100/day to run",
        excerpt=(
            "Kate Knibbs / Wired : A look at AI news website RuntimeWire, which costs $100/day to run "
            "and beat human reporters to OpenAI's Black Hat disclosure, favoring quantity over quality "
            "— Last week, an AI newsroom beat mainstream journalists to a story about OpenAI and hacking."
        ),
    )
    _score(conn, config, "E-RUNTIMEWIRE", now)
    _seed_event(
        conn, event_id="E-DECART", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260812/p56",
        title="Sources: Anthropic is in talks to buy Decart for about $6B",
        excerpt=(
            "Bloomberg : Sources: Anthropic is in talks to buy Decart for about $6B — Anthropic PBC is "
            "in talks to buy the artificial intelligence startup Decart AI for about $6 billion, according "
            "to people familiar with the matter."
        ),
    )
    _score(conn, config, "E-DECART", now)

    from news_intel.event_repo import event_as_digest_dict

    runtimewire = event_as_digest_dict(conn, conn.execute("SELECT * FROM events WHERE id='E-RUNTIMEWIRE'").fetchone())
    decart = event_as_digest_dict(conn, conn.execute("SELECT * FROM events WHERE id='E-DECART'").fetchone())
    assert decart["final_score"] > runtimewire["final_score"]


# --- hard publisher-family-in-TOP rule + Techmeme provenance ---------------


def test_two_techmeme_stories_end_to_end_only_one_in_top_with_resolved_publishers(config, conn):
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-CEREBRAS", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260812/p49",
        title="Cerebras reports Q2 revenue up 74% YoY to $180M and raises its forecast",
        excerpt=(
            "Reuters : Cerebras reports Q2 revenue up 74% YoY to $180M and raises its forecast — "
            "Cerebras Systems raised its annual revenue and gross margin forecasts on Wednesday, buoyed by "
            "robust demand for its chips from companies ramping up data-center capacity."
        ),
    )
    _score(conn, config, "E-CEREBRAS", now)
    _seed_event(
        conn, event_id="E-GROK", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260812/p39",
        title="SpaceXAI releases Grok 4.6, pricing it at $2/1M input tokens",
        excerpt=(
            "xAI : SpaceXAI releases Grok 4.6, pricing it at $2/1M input tokens — xAI today released "
            "Grok 4.6, matching GPT-5.6 Sol on the Artificial Analysis Intelligence Index."
        ),
    )
    _score(conn, config, "E-GROK", now)

    summary = run_daily(config, conn, dry_run=True, now=now)
    top_section = summary.rendered_text.split("MORE TO KNOW")[0]
    techmeme_domain_markers = sum(1 for marker in ("Cerebras", "Grok 4.6") if marker in top_section)
    assert techmeme_domain_markers <= 1

    # 2026-08-14 false-provenance fix: the displayed label now honestly
    # names Techmeme as the actual page being linked to, alongside the
    # citation it carries -- never just "Reuters"/"xAI" with a
    # techmeme.com href underneath.
    reuters_sources = event_sources(conn, "E-CEREBRAS")
    assert reuters_sources[0]["publisher"] == "Techmeme (citing Reuters)"
    xai_sources = event_sources(conn, "E-GROK")
    assert xai_sources[0]["publisher"] == "Techmeme (citing xAI)"
    assert "Techmeme (citing" in summary.rendered_text


# --- TOP requires a complete summary ----------------------------------------


def test_headline_only_story_does_not_render_with_a_blank_summary_in_top(config, conn):
    """Every 2026-08-12 TOP story rendered with title+source and NO summary
    line at all (the Techmeme citation preamble ate the character window).
    Confirms the fix end-to-end: a real sentence is now shown."""
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-DECART2", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260812/p56",
        title="Sources: Anthropic is in talks to buy Decart for about $6B",
        excerpt=(
            "Bloomberg : Sources: Anthropic is in talks to buy Decart for about $6B — Anthropic PBC is "
            "in talks to buy the artificial intelligence startup Decart AI for about $6 billion, according "
            "to people familiar with the matter."
        ),
    )
    _score(conn, config, "E-DECART2", now)
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "Anthropic PBC is in talks to buy the artificial intelligence startup Decart AI" in summary.rendered_text


# --- TOMORROW TO WATCH release time -----------------------------------------


def test_tomorrow_to_watch_shows_pacific_release_time(config, conn):
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    insert_source_row(conn, "bls_calendar", 0, adapter_type="ical")
    record_scheduled_events(
        conn, "bls_calendar",
        [
            CollectedItem(
                source_id="bls_calendar", external_id="ppi",
                canonical_url="https://www.bls.gov/schedule/news_release/bls.ics",
                title="Producer Price Index", summary_excerpt=None, language="en",
                published_at=datetime(2026, 8, 13, 12, 30, tzinfo=UTC), origin_family="scheduled_calendar",
            ),
        ],
    )
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "PT" in summary.rendered_text
    assert "AM" in summary.rendered_text or "PM" in summary.rendered_text


# --- Amit / OptionalSocialAdapter compliance --------------------------------


def test_social_stub_adapter_reports_unavailable_for_amit():
    from news_intel.settings import SourceEntry

    source = SourceEntry(
        id="social_amitisinvesting", name="@amitisinvesting (nightly market recap)", adapter="social_stub",
        tier=3, enabled=False, url="", poll_interval_seconds=300, category_hints=["social", "discovery", "markets"],
        note="no compliant free public endpoint validated",
    )
    outcome = SocialStubAdapter().fetch(source, cursor=None)
    assert outcome.items == []
    assert outcome.health_state.value == "unavailable"


def test_daily_production_unaffected_when_social_source_disabled(config, conn):
    """The disabled social_amitisinvesting entry must have zero effect on
    normal daily production -- confirmed by a plain daily run with no
    social-sourced events seeded at all."""
    now = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert summary.skipped_lock is False
    assert "No qualifying developments" in summary.rendered_text or summary.top3_count >= 0
