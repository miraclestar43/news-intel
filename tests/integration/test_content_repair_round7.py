"""2026-08-16 content-quality repair, round 7: the empty-TOP regression,
Techmeme "Sources:" title preambles, and the Aug 10 Point2/SDxCentral
freshness regression -- reproducing the exact Aug 15 production defects
(three real candidates whose excerpts had no terminal punctuation left
TOP entirely empty while all three sat in MORE TO KNOW; two titles kept
Techmeme's own "Sources:" aggregation cue; a five-day-old SDxCentral
article was presented as Aug 15 news)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.jobs.daily import _is_fresh, run_daily
from tests.integration.test_content_repair_round3 import _score, _seed_event

_MANUS_EXCERPT = (
    "Zijing Wu / Financial Times : Sources: China plans to soon lift a travel ban on Manus "
    "founders as the company unwinds its $2B acquisition by Meta; CEO Xiao Hong plans to return "
    "to Singapore —\xa0 Chief executive Xiao Hong plans to return to Singapore as AI company "
    "nears resolution of Meta separation"
)
_MANUS_TITLE = (
    "Sources: China plans to soon lift a travel ban on Manus founders as the company unwinds "
    "its $2B acquisition by Meta; CEO Xiao Hong plans to return to Singapore (Zijing Wu/Financial Times)"
)
_SB_ENERGY_EXCERPT = (
    "The Information : Sources: Nvidia in talks to invest up to $3B in SoftBank-backed data "
    "center developer SB Energy that aims to IPO soon and is behind a huge OpenAI campus in "
    "Ohio —\xa0 Nvidia is in talks to invest as much as $3 billion in SB Energy, the Softbank "
    "Group-backed developer of a massive planned Ohio …"
)
_SB_ENERGY_TITLE = (
    "Sources: Nvidia in talks to invest up to $3B in SoftBank-backed data center developer SB "
    "Energy that aims to IPO soon and is behind a huge OpenAI campus in Ohio (The Information)"
)
_POINT2_EXCERPT = (
    "Giacomo Lee / SDxCentral : Point2 Technology, which develops RF-based data center "
    "interconnection tech, raised a $136M Series B from LB Investment, Arm, Maverick Silicon, "
    "and others —\xa0 Data center interconnect disruptor Point2 Technology celebrates Series B "
    "success\xa0 —\xa0 AI data center interconnect specialist Point2 …"
)
_POINT2_TITLE = (
    "Point2 Technology, which develops RF-based data center interconnection tech, raised a "
    "$136M Series B from LB Investment, Arm, Maverick Silicon, and others (Giacomo Lee/SDxCentral)"
)


def test_empty_top_regression_end_to_end(config, conn):
    """Exact Aug 15 reproduction: all three real candidates now clear
    render-readiness (2026-08-16 summary-fallback relaxation) and TOP is
    no longer empty; titles no longer carry the "Sources:" preamble; no
    placeholder anywhere."""
    now = datetime(2026, 8, 15, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-MANUS", category="US / China / World", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260815/p4",
        origin_url="https://www.ft.com/content/fa479d50-7c79-4b6d-99c3-3830e37c1503",
        title=_MANUS_TITLE, excerpt=_MANUS_EXCERPT,
    )
    _score(conn, config, "E-MANUS", now)
    _seed_event(
        conn, event_id="E-SBENERGY", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260815/p14",
        origin_url="https://www.theinformation.com/articles/nvidia-talks-invest-3-billion-sb-energy",
        title=_SB_ENERGY_TITLE, excerpt=_SB_ENERGY_EXCERPT,
    )
    _score(conn, config, "E-SBENERGY", now)

    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "TOP STORIES" in summary.rendered_text
    top_section = summary.rendered_text.split("MORE TO KNOW")[0]
    assert "Sources:" not in summary.rendered_text
    assert "No complete source-supported summary available" not in summary.rendered_text
    assert "China plans to soon lift a travel ban on Manus" in top_section
    assert "Nvidia in talks to invest up to $3B in SoftBank-backed" in top_section
    # not merely completion-implied -- "in talks" language preserved verbatim
    assert "in talks" in top_section.lower()


def test_stale_origin_article_excluded_from_selection(config, conn):
    """Exact Aug 15 Point2/SDxCentral reproduction: origin_published_at
    verified as 2026-08-10 (five days before the Aug 15 window) excludes
    the candidate even though Techmeme's own roundup entry (and this
    project's stored published_at) carries today's date."""
    now = datetime(2026, 8, 15, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-POINT2", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260815/p6",
        origin_url="https://www.sdxcentral.com/news/nvidia-ai-interconnect-darling-bags-136m-funding-from-arm-others/",
        origin_published_at=datetime(2026, 8, 10, tzinfo=UTC),
        title=_POINT2_TITLE, excerpt=_POINT2_EXCERPT,
    )
    _score(conn, config, "E-POINT2", now)
    summary = run_daily(config, conn, dry_run=True, now=now)
    assert "Point2" not in summary.rendered_text


def test_unverified_origin_falls_back_to_existing_freshness_not_excluded(config, conn):
    """A paywalled/bot-blocked origin (ft.com returns a 403 with no
    metadata at all -- confirmed live) must not be structurally penalized
    just for being unverifiable: no origin_published_at means the existing
    published_at-based window membership applies unchanged, same as every
    source that never attempts this fetch."""
    now = datetime(2026, 8, 15, 22, 0, tzinfo=UTC)
    fresh_no_origin = {"sources": [{"origin_published_at": None}]}
    assert _is_fresh(fresh_no_origin, now) is True

    fresh_recent_origin = {"sources": [{"origin_published_at": (now - timedelta(hours=2)).isoformat()}]}
    assert _is_fresh(fresh_recent_origin, now) is True

    stale_origin = {"sources": [{"origin_published_at": (now - timedelta(days=5)).isoformat()}]}
    assert _is_fresh(stale_origin, now) is False
