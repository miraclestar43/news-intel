"""Phase 1 acceptance tests 4-6, 9-10: daily brief has 4-8 qualified events
with Top 3 / Chinese takeaways / sources / Tomorrow to Watch, stays under the
soft character target (or splits safely), degrades usefully when the LLM is
unavailable, and a repeated send creates exactly one delivery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from news_intel.adapters.base import CollectedItem
from news_intel.delivery.splitter import split_at_sections
from news_intel.delivery.telegram import TelegramSender
from news_intel.jobs import daily as daily_module
from news_intel.jobs.daily import run_daily
from news_intel.llm.errors import LLMUnavailableError
from news_intel.models import DailyDigestInput, DigestOutput, EventEnrichment
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.scheduled_repo import record_scheduled_events
from tests.conftest import insert_source_row

_EVENTS = [
    ("NVIDIA export control chip rule", "New export control rule affects AI chip shipments.", 0, "bis.gov"),
    ("Federal Reserve holds rates steady", "The Fed held rates steady, surprising markets.", 0, "federalreserve.gov"),
    ("TSMC advanced packaging expansion", "TSMC announced expansion of advanced packaging capacity.", 0, "tsmc.com"),
    ("OpenAI announces new model release", "OpenAI released a new foundation model this week.", 0, "openai.com"),
    ("Major bank reports trading halt", "A major exchange briefly halted trading amid volatility.", 0, "sec.gov"),
]


class _FakeProvider:
    model = "fake-mimo-v2.5"

    def compose_daily(self, inputs: DailyDigestInput) -> DigestOutput:
        top3 = [
            {
                "english_headline": ev["title"],
                "chinese_headline": ev["title"][:20],
                "what_happened": ev["excerpt"],
                "why_it_matters": "This affects markets and policy.",
                "chinese_takeaway": "中文要点：此事件影响市场和政策。",
                "sources": [s["publisher"] for s in ev["sources"]],
            }
            for ev in inputs.top3_candidates
        ]
        quick = [
            {
                "section": "Markets & Economy",
                "english_summary": ev["title"],
                "chinese_takeaway": "中文：简要说明。",
                "sources": [s["publisher"] for s in ev["sources"]],
            }
            for ev in inputs.quick_brief_candidates
        ]
        watch = [c["title"] for c in inputs.calendar_candidates]
        return DigestOutput(
            rendered_text="",
            model_used=self.model,
            fallback_used=False,
            structured={"top3": top3, "quick_briefs": quick, "tomorrow_to_watch": watch},
        )


def _seed_events(config, conn, now):
    for i, (title, excerpt, tier, origin) in enumerate(_EVENTS):
        source_id = f"seed_source_{i}"
        insert_source_row(conn, source_id, tier)
        event_id = f"E-20260731-{i:04d}"
        conn.execute(
            "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, "
            "published_anchor_at, confirmation_state) VALUES (?, ?, ?, ?, ?, 'unverified')",
            (event_id, title, now.isoformat(), now.isoformat(), now.isoformat()),
        )
        cur = conn.execute(
            "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
            "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
            (source_id, f"ext{i}", f"https://{origin}/{i}", f"hash{i}", title, title.lower(), excerpt,
             now.isoformat(), f"content{i}", origin),
        )
        raw_id = cur.lastrowid
        conn.execute(
            "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
            "VALUES (?, ?, 1, '[]')",
            (event_id, raw_id),
        )
        update_confirmation_state(conn, event_id)
        scores = score_event(conn, event_id, config, is_new_event=True, now=now)
        weights = config.policy.scoring.weights.model_dump()
        persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)

    insert_source_row(conn, "bls_calendar", 0, adapter_type="ical")
    record_scheduled_events(
        conn,
        "bls_calendar",
        [
            CollectedItem(
                source_id="bls_calendar",
                external_id="cpi-aug",
                canonical_url="https://www.bls.gov/schedule/news_release/cpi.htm",
                title="CPI release — August",
                summary_excerpt=None,
                language="en",
                published_at=now + timedelta(hours=20),
                origin_family="scheduled_calendar",
            )
        ],
    )


def test_daily_brief_has_qualified_events_and_chinese_takeaways(config, conn, monkeypatch):
    now = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    _seed_events(config, conn, now)
    monkeypatch.setattr(daily_module, "get_provider", lambda cfg: _FakeProvider())

    summary = run_daily(config, conn, dry_run=True, now=now)

    assert 4 <= (summary.top3_count + summary.quick_brief_count) <= 8
    assert summary.top3_count <= 3
    assert summary.fallback_used is False
    assert "TOP 3" in summary.rendered_text
    assert "中文要点" in summary.rendered_text or "中文" in summary.rendered_text
    assert "TOMORROW TO WATCH" in summary.rendered_text
    assert "CPI release" in summary.rendered_text


def test_daily_brief_stays_under_soft_char_target_or_splits(config, conn, monkeypatch):
    now = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    _seed_events(config, conn, now)
    monkeypatch.setattr(daily_module, "get_provider", lambda cfg: _FakeProvider())

    summary = run_daily(config, conn, dry_run=True, now=now)
    max_chars = config.policy.digests.telegram_hard_limit_chars
    chunks = split_at_sections(summary.rendered_text, max_chars)
    assert all(len(c) <= max_chars for c in chunks)


def test_daily_brief_falls_back_when_llm_unavailable(config, conn, monkeypatch):
    now = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    _seed_events(config, conn, now)
    # No LLM configured in `config` fixture -> get_provider() returns NullProvider,
    # whose compose_daily() always raises LLMUnavailableError.

    summary = run_daily(config, conn, dry_run=True, now=now)

    assert summary.fallback_used is True
    assert "This brief uses rule-ranked source summaries." in summary.rendered_text
    assert summary.top3_count + summary.quick_brief_count >= 4


class _FakeStoryEnrichProvider:
    """2026-08-24: compose_daily unimplemented (matches HermesCliProvider's
    real behavior) so run_daily falls into the per-story editorial path;
    enrich_events echoes back a valid, fully evidence-grounded enrichment
    for each offered event -- no invented numbers/names, cited_fact_ids
    drawn only from what was actually offered."""

    model = "fake-story-llm"

    def compose_daily(self, inputs):
        raise LLMUnavailableError("whole-digest composition not used by this provider")

    def enrich_events(self, events):
        results = []
        for event in events:
            fact_ids = [f.id for f in event.facts]
            category = event.category_candidates[0] if event.category_candidates else "Technology"
            source_ids = [s["id"] for s in event.sources]
            results.append(EventEnrichment(
                event_id=event.event_id, category=category, entities=[],
                english_headline=event.candidate_title[:140],
                what_happened=f"{event.candidate_title}, per the source reporting.",
                why_it_matters="This is significant for the affected market and policy context.",
                chinese_headline=event.candidate_title[:60],
                chinese_what_happened=f"{event.candidate_title}，据来源报道。",
                chinese_takeaway="这一事件对相关市场和政策具有重要影响。",
                significance_proposal=60, market_impact_proposal=40,
                supporting_source_ids=source_ids, cited_fact_ids=fact_ids,
            ))
        return results


def test_daily_brief_uses_per_story_editorial_layer_when_configured(config, conn, monkeypatch):
    now = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    _seed_events(config, conn, now)
    monkeypatch.setattr(daily_module, "build_bounded_provider", lambda cfg: _FakeStoryEnrichProvider())

    summary = run_daily(config, conn, dry_run=True, now=now)

    assert summary.fallback_used is False
    assert summary.model_used == "fake-story-llm"
    assert "per the source reporting." in summary.rendered_text
    assert "This is significant for the affected market and policy context." in summary.rendered_text


def test_repeated_send_creates_one_delivery(config, conn):
    async def _send_twice():
        sender = TelegramSender(bot_token=None, allowed_chat_ids=[555])
        r1 = await sender.send(
            conn,
            chat_id=555,
            text="hello world",
            kind="daily",
            idempotency_key="daily:2026-07-31:abc",
            max_chars=4000,
            dry_run=True,
        )
        r2 = await sender.send(
            conn,
            chat_id=555,
            text="hello world",
            kind="daily",
            idempotency_key="daily:2026-07-31:abc",
            max_chars=4000,
            dry_run=True,
        )
        return r1, r2

    r1, r2 = asyncio.run(_send_twice())
    assert r1.already_sent is False
    assert r2.already_sent is True

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE idempotency_key = ?", ("daily:2026-07-31:abc",)
    ).fetchone()["n"]
    assert count == 1
