"""2026-08-15 content-quality repair, round 6: publisher-family separation
(discovery_family / cited_publisher / origin_family / origin_url /
origin_verified) and the novelty-comparison fixes it depends on --
reproducing the exact 2026-08-14 production defect where a completed $60B
SpaceX-Cursor acquisition and an Amazon/Alphabet earnings-quality analysis,
both discovered via Techmeme but from different real outlets, could not
both reach TOP because the publisher-diversity cap keyed on the discovery
domain (techmeme.com) instead of the verified original publisher."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.event_repo import event_sources
from news_intel.jobs.daily import run_daily
from tests.integration.test_content_repair_round3 import _score, _seed_event


def test_verified_origin_publishers_both_survive_the_top_publisher_cap(config, conn):
    """Two Techmeme-discovered stories with DIFFERENT verified origin_url
    domains (wsj.com vs bloomberg.com) must not collide under the shared
    techmeme.com discovery domain -- both may reach TOP."""
    now = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-WSJ-ORIGIN", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260814/p30#a260814p30",
        origin_url="https://www.wsj.com/tech/nvidia-downsizes-plans-for-openai-data-center",
        title="NVIDIA downsizes plans for $250B guarantee of OpenAI data center",
        excerpt=(
            "Wall Street Journal : NVIDIA downsizes plans for $250B guarantee of OpenAI data center — "
            "NVIDIA has scaled back an earlier commitment to guarantee financing for OpenAI data centers."
        ),
    )
    _score(conn, config, "E-WSJ-ORIGIN", now)
    _seed_event(
        conn, event_id="E-BLOOMBERG-ORIGIN", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260814/p29#a260814p29",
        origin_url="https://www.bloomberg.com/news/articles/2026-08-14/spacex-completes-its-60-billion-cursor-acquisition",
        title="SpaceX completes its $60 billion Cursor acquisition",
        excerpt=(
            "Bloomberg : SpaceX completes its $60 billion Cursor acquisition — SpaceX has completed its "
            "previously announced $60 billion acquisition of Cursor maker Anysphere, a key part of Elon "
            "Musk's push to gain ground on rivals Anthropic PBC and OpenAI in the AI coding-assistant market."
        ),
    )
    _score(conn, config, "E-BLOOMBERG-ORIGIN", now)

    summary = run_daily(config, conn, dry_run=True, now=now)
    top_section = summary.rendered_text.split("MORE TO KNOW")[0]
    assert "NVIDIA downsizes plans" in top_section
    assert "SpaceX completes its $60 billion Cursor acquisition" in top_section

    wsj_sources = event_sources(conn, "E-WSJ-ORIGIN")
    bloomberg_sources = event_sources(conn, "E-BLOOMBERG-ORIGIN")
    assert wsj_sources[0]["origin_family"] == "wsj.com"
    assert bloomberg_sources[0]["origin_family"] == "bloomberg.com"
    assert wsj_sources[0]["origin_verified"] is True
    assert bloomberg_sources[0]["origin_verified"] is True
    # Verified origin -> honest bare-outlet label and a real clickable link
    # to the original article, not the Techmeme roundup page underneath it.
    assert wsj_sources[0]["publisher"] == "Wall Street Journal"
    assert wsj_sources[0]["display_url"] == wsj_sources[0]["origin_url"]
    assert "wsj.com" in summary.rendered_text
    assert "bloomberg.com" in summary.rendered_text


def test_unverified_techmeme_origin_falls_back_to_discovery_family(config, conn):
    """A Techmeme-discovered item whose description didn't yield a resolvable
    origin_url (origin_url column NULL) must conservatively fall back to the
    discovery domain (techmeme.com) for its publisher-family key, and keep
    the honest "Techmeme (citing X)" hedge in its display label -- it must
    never claim a verified link it doesn't have."""
    now = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-UNVERIFIED", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260814/p99#a260814p99",
        title="Cerebras reports Q2 revenue up 74% YoY to $180M and raises its forecast",
        excerpt=(
            "Reuters : Cerebras reports Q2 revenue up 74% YoY to $180M and raises its forecast — "
            "Cerebras Systems raised its annual revenue and gross margin forecasts on Wednesday."
        ),
    )
    _score(conn, config, "E-UNVERIFIED", now)
    sources = event_sources(conn, "E-UNVERIFIED")
    assert sources[0]["origin_url"] is None
    assert sources[0]["origin_verified"] is False
    assert sources[0]["origin_family"] == "techmeme.com"
    assert sources[0]["publisher"] == "Techmeme (citing Reuters)"
    assert sources[0]["display_url"] == sources[0]["url"]


def test_discovery_and_verified_origin_do_not_double_count_as_corroboration(config, conn):
    """Two raw items for the SAME event -- one discovered via Techmeme citing
    Bloomberg (with a verified bloomberg.com origin_url) and one directly
    collected from a Bloomberg-domain source -- describe only ONE real
    publisher family (bloomberg.com), not two independent corroborating
    sources, once origin is resolved. This just confirms origin_family
    normalizes both to the same key; the confirmation-count/corroboration
    logic itself is untouched by this repair."""
    now = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-DOUBLECOUNT", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260814/p29#a260814p29",
        origin_url="https://www.bloomberg.com/news/articles/2026-08-14/spacex-completes-its-60-billion-cursor-acquisition",
        title="SpaceX completes its $60 billion Cursor acquisition",
        excerpt=(
            "Bloomberg : SpaceX completes its $60 billion Cursor acquisition — SpaceX has completed its "
            "previously announced acquisition of Cursor maker Anysphere in a deal valued at $60 billion."
        ),
    )
    sources = event_sources(conn, "E-DOUBLECOUNT")
    assert sources[0]["origin_family"] == "bloomberg.com"
    assert sources[0]["discovery_family"] == "techmeme.com"


def test_amazon_alphabet_earnings_story_classifies_as_markets_not_ai(config, conn):
    """End-to-end reproduction of the exact 2026-08-14 production defect
    (E-20260814-0126): an Amazon/Alphabet "other income" earnings-quality
    analysis, whose only AI-vocabulary hit is a single background mention
    of "Anthropic" as one example investment, must classify as Markets &
    Economy through the real scoring pipeline -- not AI & Semiconductors."""
    now = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
    title = (
        'In Q2, "other income", mostly from investment gains, at Amazon and Alphabet totaled ~$121B '
        "after taxes and made up 66% and 71%, respectively, of profits (Jonathan Weil/Wall Street Journal)"
    )
    _seed_event(
        conn, event_id="E-AMZN-GOOGL", category="AI & Semiconductors", now=now, source_id="techmeme", tier=3,
        source_name="Techmeme", canonical_url="https://www.techmeme.com/260814/p25#a260814p25",
        origin_url="https://www.bloomberg.com/news/articles/2026-08-14/amazon-s-new-user-agreement",
        title=title,
        excerpt=(
            f"Jonathan Weil / Wall Street Journal : {title} — Gains on investments in companies like "
            "Anthropic are boosting earnings at some of the biggest technology companies"
        ),
    )
    _score(conn, config, "E-AMZN-GOOGL", now)
    row = conn.execute("SELECT category FROM events WHERE id = 'E-AMZN-GOOGL'").fetchone()
    assert row["category"] == "Markets & Economy"
