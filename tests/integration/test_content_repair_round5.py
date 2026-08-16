"""2026-08-14 reconciliation round: Applied Materials outranking the GPT
builder guide and Regions Bank enforcement action end-to-end, source-label/
domain consistency, and summary-enrichment ordering, reproducing the exact
2026-08-13 production defects."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from news_intel.event_repo import event_sources
from news_intel.jobs.daily import run_daily
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from tests.conftest import insert_source_row


def _seed_event(
    conn, *, event_id, title, excerpt, now, source_id, tier=0, source_name=None,
    origin_family="example.test", canonical_url=None,
):
    insert_source_row(conn, source_id, tier, name=source_name)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, 'unverified')",
        (event_id, title, now.isoformat(), now.isoformat(), now.isoformat()),
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
    update_confirmation_state(conn, event_id)


def _score(conn, config, event_id, now):
    scores = score_event(conn, event_id, config, is_new_event=True, now=now)
    weights = config.policy.scoring.weights.model_dump()
    persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)


def test_applied_materials_outranks_builder_guide_and_regions_bank_end_to_end(config, conn):
    """The exact 2026-08-13 reproduction: a T0-sourced tutorial and a T0-
    sourced individual-employee enforcement action must not outrank a
    genuine, single-sourced semiconductor earnings beat."""
    now = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-GPT-GUIDE", source_id="openai_news", source_name="OpenAI News",
        title="The builder's guide to GPT-5.6",
        excerpt=(
            "Learn how startups use GPT-5.6 to build faster, more cost-efficient AI agents with "
            "smarter model selection and new Responses API capabilities."
        ),
        now=now,
    )
    _score(conn, config, "E-GPT-GUIDE", now)
    _seed_event(
        conn, event_id="E-REGIONS", source_id="fed_press_all", source_name="Federal Reserve — all press",
        title="Federal Reserve Board issues enforcement action with former employee of Regions Bank",
        excerpt="The Federal Reserve Board announced an enforcement action against a former employee of Regions Bank.",
        now=now,
    )
    _score(conn, config, "E-REGIONS", now)
    _seed_event(
        conn, event_id="E-APPLIED-MATERIALS", source_id="techmeme", tier=3, source_name="Techmeme",
        canonical_url="https://www.techmeme.com/260813/p53",
        title=(
            "Applied Materials reports Q3 revenue up 25% YoY to $9.12B, vs. $8.99B est., net income up "
            "43% to $2.54B, and forecasts Q4 revenue above estimates (Elias Schisgall/Wall Street Journal)"
        ),
        excerpt=(
            "Elias Schisgall / Wall Street Journal : Applied Materials reports Q3 revenue up 25% YoY to "
            "$9.12B, vs. $8.99B est., net income up 43% to $2.54B, and forecasts Q4 revenue above "
            "estimates — The company is looking to ramp up its manufacturing capacity to meet continued "
            "semiconductor solutions demand."
        ),
        now=now,
    )
    _score(conn, config, "E-APPLIED-MATERIALS", now)

    summary = run_daily(config, conn, dry_run=True, now=now)

    assert "Applied Materials" in summary.rendered_text
    assert "builder" not in summary.rendered_text.lower()
    assert "Regions Bank" not in summary.rendered_text
    top_section = summary.rendered_text.split("MORE TO KNOW")[0]
    assert "Applied Materials" in top_section  # must be in TOP, not demoted


def test_source_label_domain_consistency_no_false_provenance(config, conn):
    """The exact 2026-08-13 defect: "Source: Wall Street Journal" pointing
    at a techmeme.com href. Every rendered source link's visible text must
    honestly reflect the domain it actually points to."""
    now = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-AM2", source_id="techmeme", tier=3, source_name="Techmeme",
        canonical_url="https://www.techmeme.com/260813/p53",
        title="Applied Materials reports Q3 revenue up 25% YoY to $9.12B (Elias Schisgall/Wall Street Journal)",
        excerpt=(
            "Elias Schisgall / Wall Street Journal : Applied Materials reports Q3 revenue up 25% YoY to "
            "$9.12B — The company is ramping up manufacturing capacity to meet continued demand."
        ),
        now=now,
    )
    _score(conn, config, "E-AM2", now)
    sources = event_sources(conn, "E-AM2")
    label, url = sources[0]["publisher"], sources[0]["url"]
    assert urlparse(url).netloc.endswith("techmeme.com")
    assert "Techmeme" in label  # label always names the domain it actually links to
    assert "Wall Street Journal" in label  # the citation is still surfaced, just honestly framed


def test_render_readiness_gate_drops_unsummarizable_story_and_backfills_top(config, conn):
    """2026-08-16 repair: a high-decision-value story whose excerpt yields
    no complete sentence/fallback clause from any source must NOT render
    with the old internal-diagnostic placeholder in TOP (not reader-facing
    content), and -- per the exact 2026-08-15 regression, where three
    render-failed candidates left TOP entirely empty while still occupying
    MORE TO KNOW -- it must not be silently demoted into MORE TO KNOW
    either. It is dropped from delivery entirely, and the next qualified,
    render-ready story backfills the vacated TOP slot. The principle this
    always protected -- decision-value ordering is computed before, and
    unaffected by, summary availability -- is unchanged and covered by
    tests/unit/test_daily_selection_and_formatting.py's render-readiness
    gate tests."""
    now = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-HIGH-VALUE-NO-SUMMARY", source_id="techmeme", tier=3, source_name="Techmeme",
        title="NVIDIA data center supplier reports Q3 revenue up 90% YoY to $300M and raises forecasts",
        excerpt=(
            "Reuters : NVIDIA data center supplier reports Q3 revenue up 90% YoY to $300M and raises "
            "forecasts sharply — no period here"
        ),
        now=now,
    )
    _score(conn, config, "E-HIGH-VALUE-NO-SUMMARY", now)
    _seed_event(
        conn, event_id="E-BACKFILL-CANDIDATE", source_id="techmeme", tier=3, source_name="Techmeme",
        canonical_url="https://www.techmeme.com/260813/p61",
        title="Cerebras reports Q2 revenue up 74% YoY to $180M and raises its forecast",
        excerpt=(
            "Reuters : Cerebras reports Q2 revenue up 74% YoY to $180M and raises its forecast — "
            "Cerebras Systems raised its annual revenue and gross margin forecasts on Wednesday."
        ),
        now=now,
    )
    _score(conn, config, "E-BACKFILL-CANDIDATE", now)
    _seed_event(
        conn, event_id="E-LOW-VALUE-CLEAN-SUMMARY", source_id="openai_news", source_name="OpenAI News",
        title="A guide to prompt engineering",
        excerpt="This guide explains helpful prompt engineering tips for developers.",
        now=now,
    )
    _score(conn, config, "E-LOW-VALUE-CLEAN-SUMMARY", now)

    summary = run_daily(config, conn, dry_run=True, now=now)
    top_section = summary.rendered_text.split("MORE TO KNOW")[0]
    assert "No complete source-supported summary available" not in summary.rendered_text
    assert "NVIDIA data center supplier" not in summary.rendered_text  # dropped, not demoted anywhere
    assert "Cerebras" in top_section  # backfilled the vacated TOP slot
    # the low-value tutorial fails the event-type gate entirely
    assert "prompt engineering" not in summary.rendered_text.lower()
