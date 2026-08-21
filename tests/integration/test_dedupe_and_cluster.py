"""Covers acceptance test 2 (Phase 1): two differently-titled articles about
one event cluster into a single event, while two unrelated stories that
merely share a company name remain separate. Also covers acceptance test 3:
re-running collection (insert_raw_item) is idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.adapters.base import CollectedItem
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.dedupe import UpsertResult, upsert_raw_item
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from tests.conftest import insert_source_row


def _make_and_store(conn, config, *, source_id, tier, title, excerpt, url, now):
    insert_source_row(conn, source_id, tier)
    item = CollectedItem(
        source_id=source_id,
        external_id=url,
        canonical_url=url,
        title=title,
        summary_excerpt=excerpt,
        language="en",
        published_at=now,
        origin_family=source_id,
        metadata={},
    )
    normalized = normalize(item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now)
    upsert = upsert_raw_item(conn, normalized)
    known = known_entities(config.topics.groups)
    event_id, is_new = find_or_create_event(
        conn, normalized, upsert.raw_item_id, config.policy.dedupe, known, now=now
    )
    return upsert.raw_item_id, event_id, is_new


def test_two_titles_about_one_event_cluster(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    _, event_a, is_new_a = _make_and_store(
        conn,
        config,
        source_id="nvidia_newsroom",
        tier=0,
        title="NVIDIA Announces New AI Chip Export Rules Compliance Update",
        excerpt="NVIDIA today announced changes to comply with updated export control rules for AI chips.",
        url="https://nvidianews.nvidia.com/releases/ai-chip-export-rules-update",
        now=now,
    )
    assert is_new_a is True

    _, event_b, is_new_b = _make_and_store(
        conn,
        config,
        source_id="techmeme",
        tier=3,
        title="NVIDIA Announces Compliance Update For New AI Chip Export Rules",
        excerpt="Sources say NVIDIA is updating its AI chip shipments to comply with new export control rules.",
        url="https://example-news.test/nvidia-export-compliance-update",
        now=now,
    )
    assert is_new_b is False
    assert event_a == event_b

    sources_count = conn.execute(
        "SELECT COUNT(*) AS n FROM event_sources WHERE event_id = ?", (event_a,)
    ).fetchone()["n"]
    assert sources_count == 2


def test_unrelated_same_company_stories_stay_separate(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    _, event_a, _ = _make_and_store(
        conn,
        config,
        source_id="nvidia_newsroom",
        tier=0,
        title="NVIDIA Announces New AI Chip Export Rules Compliance Update",
        excerpt="NVIDIA today announced changes to comply with updated export control rules for AI chips.",
        url="https://nvidianews.nvidia.com/releases/ai-chip-export-rules-update",
        now=now,
    )
    _, event_b, is_new_b = _make_and_store(
        conn,
        config,
        source_id="nvidia_newsroom",
        tier=0,
        title="NVIDIA Reports Quarterly Earnings Ahead of Expectations",
        excerpt="NVIDIA reported quarterly revenue ahead of analyst expectations, driven by data center demand.",
        url="https://nvidianews.nvidia.com/releases/q2-earnings-2026",
        now=now,
    )

    assert is_new_b is True
    assert event_a != event_b


def test_cross_edition_same_url_does_not_suppress_other_edition(config, conn):
    """2026-08-21 repair regression: gov_cn_policy (en_global) and
    gov_cn_policy_zh (zh_cn) scrape the identical gov.cn URL. Before the
    edition_id scoping fix, gov_cn_policy_zh's item silently matched
    gov_cn_policy's already-stored row (same url_hash/content_hash) and
    returned UNCHANGED -- the zh_cn edition never got its own raw_item/event
    for this source at all, regardless of how many times it was fetched."""
    now = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
    url = "https://www.gov.cn/zhengce/content/202608/content_9999999.htm"
    title = "国务院关于修改《住房公积金管理条例》的决定"

    insert_source_row(conn, "gov_cn_policy", 0)
    en_item = CollectedItem(
        source_id="gov_cn_policy",
        external_id=url,
        canonical_url=url,
        title=title,
        summary_excerpt=None,
        language="zh",
        published_at=now,
        origin_family="gov_cn_policy",
        metadata={},
    )
    en_normalized = normalize(
        en_item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now, edition_id="en_global"
    )
    en_upsert = upsert_raw_item(conn, en_normalized)
    assert en_upsert.result == UpsertResult.NEW

    insert_source_row(conn, "gov_cn_policy_zh", 0)
    zh_item = CollectedItem(
        source_id="gov_cn_policy_zh",
        external_id=url,
        canonical_url=url,
        title=title,
        summary_excerpt=None,
        language="zh",
        published_at=now,
        origin_family="gov_cn_policy_zh",
        metadata={},
    )
    zh_normalized = normalize(
        zh_item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now, edition_id="zh_cn"
    )
    zh_upsert = upsert_raw_item(conn, zh_normalized)
    assert zh_upsert.result == UpsertResult.NEW
    assert zh_upsert.raw_item_id != en_upsert.raw_item_id

    zh_row = conn.execute(
        "SELECT edition_id FROM raw_items WHERE id = ?", (zh_upsert.raw_item_id,)
    ).fetchone()
    assert zh_row["edition_id"] == "zh_cn"


def test_rerunning_collection_is_idempotent(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    insert_source_row(conn, "nvidia_newsroom", 0)

    item = CollectedItem(
        source_id="nvidia_newsroom",
        external_id="https://nvidianews.nvidia.com/releases/ai-chip-export-rules-update",
        canonical_url="https://nvidianews.nvidia.com/releases/ai-chip-export-rules-update",
        title="NVIDIA Announces New AI Chip Export Rules Compliance Update",
        summary_excerpt="NVIDIA today announced changes to comply with updated export control rules.",
        language="en",
        published_at=now,
        origin_family="nvidia_newsroom",
        metadata={},
    )
    normalized = normalize(item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now)

    first = upsert_raw_item(conn, normalized)
    second = upsert_raw_item(conn, normalized)
    assert first.raw_item_id == second.raw_item_id
    assert first.result == UpsertResult.NEW
    assert second.result == UpsertResult.UNCHANGED  # identical replay is a no-op, not a second insert

    count = conn.execute("SELECT COUNT(*) AS n FROM raw_items").fetchone()["n"]
    assert count == 1
