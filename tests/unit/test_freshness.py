"""Freshness state machine tests (2026-08-17 repair). See
pipeline/freshness.py's docstring for the full state-machine contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.pipeline.freshness import (
    BOUNDED_UNVERIFIED,
    INSUFFICIENT_FRESHNESS_EVIDENCE,
    VERIFIED_FRESH,
    VERIFIED_STALE,
    assess_freshness,
)

from ..conftest import insert_source_row

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def _insert_raw_item(conn, *, id_, canonical_url, origin_url, normalized_title, origin_family, fetched_at):
    conn.execute(
        "INSERT INTO raw_items (id, source_id, canonical_url, url_hash, title, normalized_title, "
        "published_at, fetched_at, content_hash, origin_family, origin_url) "
        "VALUES (?, 'test_source', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id_, canonical_url, f"hash{id_}", normalized_title, normalized_title, fetched_at.isoformat(),
         fetched_at.isoformat(), f"chash{id_}", origin_family, origin_url),
    )


def _item(**overrides) -> dict:
    primary = {
        "id": "S1",
        "url": "https://aggregator.example.com/roundup/1",
        "origin_url": "https://paywalled.example.com/article-1",
        "origin_family": "paywalled.example.com",
        "raw_title": "Some Original Headline About A Company",
        "published_at": NOW.isoformat(),
        "fetched_at": NOW.isoformat(),
        "origin_published_at": None,
        "origin_verified": False,
    }
    primary.update(overrides)
    return {"sources": [primary]}


def test_verified_origin_within_buffer_is_verified_fresh(conn):
    item = _item(origin_published_at=(NOW - timedelta(hours=2)).isoformat())
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == VERIFIED_FRESH
    assert result.is_fresh


def test_verified_origin_outside_buffer_is_verified_stale(conn):
    item = _item(origin_published_at=(NOW - timedelta(days=6)).isoformat())
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == VERIFIED_STALE
    assert not result.is_fresh


def test_blocked_origin_with_no_aggregator_time_is_insufficient_evidence(conn):
    item = _item(published_at=None)
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == INSUFFICIENT_FRESHNESS_EVIDENCE
    assert not result.is_fresh


def test_blocked_origin_with_stale_aggregator_time_is_insufficient_evidence(conn):
    item = _item(published_at=(NOW - timedelta(days=6)).isoformat())
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == INSUFFICIENT_FRESHNESS_EVIDENCE


def test_blocked_origin_with_stale_first_seen_is_insufficient_evidence(conn):
    item = _item()
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW - timedelta(days=6))
    assert result.state == INSUFFICIENT_FRESHNESS_EVIDENCE


def test_blocked_origin_with_no_stored_history_is_bounded_unverified(conn):
    """The reproduction case this repair targets: an aggregator "discovers"
    (with a fresh published_at) a paywalled article that has never been
    seen before in stored history at all -- this legitimately reaches
    bounded_unverified, since every applicable safeguard passes."""
    item = _item()
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == BOUNDED_UNVERIFIED
    assert result.is_fresh


def test_blocked_origin_with_older_matching_canonical_url_is_insufficient_evidence(conn):
    """The exact Point2/SDxCentral-shaped defect this repair targets: the
    SAME origin_url was already fetched five days earlier -- direct,
    structural evidence of resurfacing, not a fresh discovery."""
    insert_source_row(conn, "test_source", tier=1)
    _insert_raw_item(
        conn, id_=999, canonical_url="https://aggregator.example.com/roundup/OLD",
        origin_url="https://paywalled.example.com/article-1",
        normalized_title="an older headline entirely",
        origin_family="paywalled.example.com", fetched_at=NOW - timedelta(days=5),
    )
    item = _item()
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == INSUFFICIENT_FRESHNESS_EVIDENCE
    assert "resurfacing" in result.reason


def test_blocked_origin_with_older_near_duplicate_title_is_insufficient_evidence(conn):
    """Catches a re-titled/re-published copy of the same old story even
    when the URL itself doesn't exactly match."""
    insert_source_row(conn, "test_source", tier=1)
    _insert_raw_item(
        conn, id_=999, canonical_url="https://aggregator.example.com/roundup/OLD-DIFFERENT-URL",
        origin_url="https://paywalled.example.com/article-1-republished",
        normalized_title="some original headline about a company",
        origin_family="paywalled.example.com", fetched_at=NOW - timedelta(days=5),
    )
    item = _item(url="https://aggregator.example.com/roundup/BRAND-NEW-URL",
                 origin_url="https://paywalled.example.com/completely-different-path")
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == INSUFFICIENT_FRESHNESS_EVIDENCE


def test_blocked_origin_with_older_unrelated_history_still_bounded_unverified(conn):
    """A DIFFERENT origin_family's older history must never contaminate
    this item's own assessment."""
    insert_source_row(conn, "test_source", tier=1)
    _insert_raw_item(
        conn, id_=999, canonical_url="https://other-aggregator.example.com/roundup/UNRELATED",
        origin_url="https://unrelated.example.com/some-other-article",
        normalized_title="a totally unrelated story",
        origin_family="unrelated.example.com", fetched_at=NOW - timedelta(days=5),
    )
    item = _item()
    result = assess_freshness(conn, item, window_end=NOW, event_first_seen_at=NOW)
    assert result.state == BOUNDED_UNVERIFIED
