"""2026-08-06 daily-canary incident repair: jobs/collect.py's CONTENT_CHANGED
path could delete a raw_item's event_sources link, recluster it onto a
*different* event, and never notice the original event was left with zero
sources -- yet the original stayed status='developing' with a frozen,
now-unbacked score, fully eligible for scoring-dependent selection. Proven
root cause (real GPT-5.6 titles from the live canary DB): a
pipeline/product_identity.py false-conflict (an ordinary prose verb like
"introduces" after "ChatGPT" is misparsed as a named-feature claim, which
then spuriously conflicts with the SAME event's own matching GPT-5.6
version) blocked a legitimate self-match in find_or_create_event, so the
new-content raw_item created a brand-new event instead of rejoining its own
prior event.

This suite does not attempt to fix the underlying clustering-similarity
algorithm (a much larger, riskier change) -- it proves the narrower,
correct fix: (1) collect.py transactionally marks an event orphaned_at the
moment reclustering leaves it with zero sources, (2) qualified_events_since
authoritatively (live EXISTS, not the denormalized flag) excludes any
zero-source event from daily selection regardless of score, (3) a fresh
source reattaching self-heals orphaned_at, and (4) the pre-existing
daily_canary.py URL assertion remains as independent defense in depth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState
from news_intel.event_repo import qualified_events_since
from news_intel.jobs import collect as collect_module
from news_intel.jobs.collect import run_collect
from news_intel.jobs.daily_canary import run_daily_canary
from news_intel.pipeline.select import select_diverse_events
from news_intel.settings import SourceEntry
from tests.conftest import insert_source_row


class _ScriptedAdapter:
    """Returns a different, pre-scripted item set on each successive call --
    simulates a source's article being edited between two collect ticks."""

    def __init__(self, *item_batches: list[CollectedItem]):
        self._batches = list(item_batches)
        self._calls = 0

    def fetch(self, ctx, cursor):
        batch = self._batches[min(self._calls, len(self._batches) - 1)]
        self._calls += 1
        return FetchOutcome(batch, f"cursor-{self._calls}", SourceHealthState.HEALTHY)


def _one_source_config(config, source_id="openai_news_test"):
    config.sources.sources.clear()
    config.sources.sources.append(
        SourceEntry(id=source_id, name="OpenAI News", adapter="rss", tier=0, enabled=True, url="https://x/1")
    )
    return source_id


def test_reproduced_upstream_failure_path_is_detected_and_excluded(config, conn, monkeypatch):
    """End-to-end reproduction of the exact incident mechanism via the real
    run_collect() pipeline, not a hand exercise of internals."""
    source_id = _one_source_config(config)

    original = CollectedItem(
        source_id=source_id, external_id="https://openai.com/index/improving-gpt-5-6-sol-in-chatgpt",
        canonical_url="https://openai.com/index/improving-gpt-5-6-sol-in-chatgpt",
        title="Improving GPT-5.6 Sol in ChatGPT—and expanding access for free users",
        summary_excerpt=None, language="en", published_at=None, origin_family="openai.com",
    )
    edited = CollectedItem(
        source_id=source_id, external_id="https://openai.com/index/improving-gpt-5-6-sol-in-chatgpt",
        canonical_url="https://openai.com/index/improving-gpt-5-6-sol-in-chatgpt",
        title="Improving GPT‑5.6 Sol in ChatGPT—and expanding access to GPT-5.6 Luna for free users",
        summary_excerpt=(
            "ChatGPT introduces improved GPT-5.6 Sol with better accuracy and consistency, plus "
            "expanded access for free users and unlimited everyday chats with GPT-5.6 Luna."
        ),
        language="en", published_at=None, origin_family="openai.com",
    )
    adapter = _ScriptedAdapter([original], [edited])
    monkeypatch.setattr(collect_module, "get_adapter", lambda adapter_type: adapter)

    t0 = datetime(2026, 8, 6, 17, 0, tzinfo=UTC)
    summary1 = run_collect(config, conn, now=t0)
    assert summary1.new_events == 1
    original_event = conn.execute("SELECT id FROM events ORDER BY id LIMIT 1").fetchone()
    original_event_id = original_event["id"]
    conn.execute(
        "UPDATE events SET final_score = 90, significance = 90, confirmation_state = 'official' WHERE id = ?",
        (original_event_id,),
    )

    t1 = t0 + timedelta(hours=1)
    summary2 = run_collect(config, conn, now=t1)
    assert summary2.changed_raw_items == 1

    # Reproduces the incident's exact symptom: the original event now has
    # zero sources (its content moved to a new event via the product-
    # identity false-conflict bug -- reclustering did NOT rejoin it).
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM event_sources WHERE event_id = ?", (original_event_id,)
    ).fetchone()["n"]
    assert remaining == 0

    # The fix: orphaned_at is set transactionally, and the event is
    # excluded from daily selection despite its high, frozen score.
    row = conn.execute("SELECT orphaned_at, final_score FROM events WHERE id = ?", (original_event_id,)).fetchone()
    assert row["orphaned_at"] is not None
    assert row["final_score"] == 90  # score genuinely is high -- exclusion is source-based, not score-based

    qualified = qualified_events_since(conn, window_start=t0 - timedelta(days=1), min_score=0)
    assert original_event_id not in {r["id"] for r in qualified}


def test_reclustering_that_still_matches_does_not_orphan_or_lose_sources(config, conn, monkeypatch):
    """The good path: a minor content edit that still legitimately matches
    the same event must not be treated as an orphan -- proves the fix does
    not harm normal reclustering-into-the-same-event."""
    source_id = _one_source_config(config)

    v1 = CollectedItem(
        source_id=source_id, external_id="https://openai.com/index/agent-plugins",
        canonical_url="https://openai.com/index/agent-plugins",
        title="OpenAI introduces Agent Plugins for developers",
        summary_excerpt="A new standard for bundling skills and MCP servers.",
        language="en", published_at=None, origin_family="openai.com",
    )
    v2 = CollectedItem(
        source_id=source_id, external_id="https://openai.com/index/agent-plugins",
        canonical_url="https://openai.com/index/agent-plugins",
        title="OpenAI introduces Agent Plugins for developers today",
        summary_excerpt="A new standard for bundling skills and MCP servers, now generally available.",
        language="en", published_at=None, origin_family="openai.com",
    )
    adapter = _ScriptedAdapter([v1], [v2])
    monkeypatch.setattr(collect_module, "get_adapter", lambda adapter_type: adapter)

    t0 = datetime(2026, 8, 6, 17, 0, tzinfo=UTC)
    run_collect(config, conn, now=t0)
    event_id = conn.execute("SELECT id FROM events ORDER BY id LIMIT 1").fetchone()["id"]

    summary2 = run_collect(config, conn, now=t0 + timedelta(minutes=5))
    assert summary2.changed_raw_items == 1
    assert summary2.updated_events == 1
    assert summary2.new_events == 0

    row = conn.execute("SELECT orphaned_at FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["orphaned_at"] is None
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM event_sources WHERE event_id = ?", (event_id,)
    ).fetchone()["n"]
    assert remaining == 1


def _seed_scored_event(conn, *, event_id, title, score, with_source, now):
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
        "confirmation_state, final_score) VALUES (?, ?, ?, ?, ?, 'official', ?)",
        (event_id, title, now.isoformat(), now.isoformat(), now.isoformat(), score),
    )
    if with_source:
        source_id = f"src_{event_id}"
        insert_source_row(conn, source_id, 0)
        cur = conn.execute(
            "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
            "summary_excerpt, language, fetched_at, content_hash, origin_family) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
            (source_id, f"{event_id}-0", f"https://x.test/{event_id}", f"{event_id}-hash", title, title.lower(),
             "excerpt", now.isoformat(), f"{event_id}-content", source_id),
        )
        conn.execute(
            "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
            "VALUES (?, ?, 1, '[]')",
            (event_id, cur.lastrowid),
        )


def test_sourceless_high_score_event_excluded_before_topn_and_next_qualified_selected(config, conn):
    now = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
    _seed_scored_event(
        conn, event_id="E-ORPHAN-HI", title="Orphaned but high-scoring", score=95, with_source=False, now=now
    )
    conn.execute("UPDATE events SET orphaned_at = ? WHERE id = 'E-ORPHAN-HI'", (now.isoformat(),))
    _seed_scored_event(conn, event_id="E-VALID-LO", title="Valid, lower-scoring", score=60, with_source=True, now=now)

    qualified = qualified_events_since(conn, window_start=now - timedelta(days=1), min_score=0)
    ids = [r["id"] for r in qualified]
    assert "E-ORPHAN-HI" not in ids
    assert "E-VALID-LO" in ids

    top3, quick = select_diverse_events(qualified, max_per_family=2, min_events=1, max_events=4, top3_threshold=50)
    selected_ids = {r["id"] for r in top3} | {r["id"] for r in quick}
    assert selected_ids == {"E-VALID-LO"}  # the invalid high-score event never displaces the next qualified one


def test_final_fail_closed_still_catches_an_orphan_if_selection_filter_is_bypassed(config, conn, monkeypatch):
    """Defense in depth: even if qualified_events_since's own filter were
    somehow bypassed (a future regression, a different call path), the
    pre-send URL assertion in jobs/daily_canary.py must independently catch
    a zero-source event before ever rendering/sending it."""
    now = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
    _seed_scored_event(
        conn, event_id="E-BYPASS-ORPHAN", title="Should never be sent", score=95, with_source=False, now=now
    )
    conn.execute("UPDATE events SET category = 'AI & Semiconductors' WHERE id = 'E-BYPASS-ORPHAN'")

    import news_intel.jobs.daily_canary as canary_module

    real_qualified_events_since = canary_module.qualified_events_since

    def _bypassed(conn_, *, window_start, min_score):
        # Simulate the orphan slipping past whatever upstream filter exists.
        rows = list(real_qualified_events_since(conn_, window_start=window_start, min_score=min_score))
        bypassed_row = conn_.execute(
            "SELECT *, NULL AS primary_origin_family FROM events WHERE id = 'E-BYPASS-ORPHAN'"
        ).fetchone()
        return [*rows, bypassed_row]

    monkeypatch.setattr(canary_module, "qualified_events_since", _bypassed)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    from news_intel.settings import reset_config_cache

    reset_config_cache()
    from pathlib import Path

    from news_intel.settings import load_config

    bypass_config = load_config(config_dir=Path("config"), data_dir=config.data_dir)
    try:
        summary = run_daily_canary(bypass_config, conn, canary_index=1, now=now)
    finally:
        reset_config_cache()

    assert summary.fail_closed is True
    assert "no mapped source URL" in summary.fail_reason
    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0


def test_repeated_collection_reprocessing_is_idempotent_and_creates_no_orphan(config, conn, monkeypatch):
    source_id = _one_source_config(config)
    item = CollectedItem(
        source_id=source_id, external_id="https://openai.com/index/stable-story",
        canonical_url="https://openai.com/index/stable-story",
        title="A stable story that never changes",
        summary_excerpt="Nothing about this story changes across ticks.",
        language="en", published_at=None, origin_family="openai.com",
    )
    monkeypatch.setattr(collect_module, "get_adapter", lambda adapter_type: _ScriptedAdapter([item]))

    t0 = datetime(2026, 8, 6, 17, 0, tzinfo=UTC)
    s1 = run_collect(config, conn, now=t0)
    assert s1.new_raw_items == 1
    event_id = conn.execute("SELECT id FROM events ORDER BY id LIMIT 1").fetchone()["id"]

    for i in range(1, 4):
        s = run_collect(config, conn, now=t0 + timedelta(minutes=5 * i))
        assert s.unchanged_raw_items == 1
        assert s.new_raw_items == 0
        assert s.changed_raw_items == 0

    row = conn.execute("SELECT orphaned_at FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["orphaned_at"] is None
    count = conn.execute("SELECT COUNT(*) AS n FROM event_sources WHERE event_id = ?", (event_id,)).fetchone()["n"]
    assert count == 1  # never duplicated across repeated unchanged ticks
