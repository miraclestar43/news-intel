"""Freshness state machine for blocked-origin items (2026-08-17 repair).

jobs/daily.py's existing _is_fresh() already handles the case where
origin_published_at WAS verified (a live fetch of the origin page
succeeded) -- see its _STALE_ORIGIN_BUFFER docstring. That verified case is
unchanged by this module and is exposed here as VERIFIED_FRESH/
VERIFIED_STALE for a single, coherent state machine.

The gap this module closes: when origin_published_at cannot be fetched at
all (paywall/bot-check -- confirmed live against ft.com, which returns a
403 "Security Verification" page with no metadata) the OLD behavior was to
treat the item as unconditionally fresh (`if not origin_published_at:
return True`). That is exactly how an aggregator "republishing" (carrying
in its own daily roundup, with today's own pubDate) a genuinely old,
paywalled article could silently pass as breaking news -- verification
failure is not evidence of freshness any more than it is evidence of
staleness.

A blocked-origin item may only reach BOUNDED_UNVERIFIED -- never treated as
fully verified -- when ALL of the following pass:
  1. the aggregator's own published_at is itself recent (within the same
     buffer used for verified staleness);
  2. the event's own first_seen_at (when this event was first ingested by
     this pipeline, at all) is recent;
  3. no OLDER raw_item exists in stored history pointing at the same
     canonical_url or origin_url (an earlier fetched_at for the identical
     URL is direct, structural evidence of resurfacing -- not a heuristic
     over article text);
  4. no OLDER raw_item exists in stored history with a near-duplicate
     normalized_title from the same origin_family (catches a
     re-published/re-titled copy of the same old story without relying on
     canonical_url matching exactly).

Anything short of that -- no aggregator timestamp at all, a stale
aggregator timestamp, a stale first-seen time, or a genuine match against
older stored history -- is INSUFFICIENT_FRESHNESS_EVIDENCE, which fails
closed exactly like VERIFIED_STALE (jobs/daily.py's _is_fresh treats
anything other than VERIFIED_FRESH/BOUNDED_UNVERIFIED as not fresh).

These states are purely an internal gating signal -- never rendered or
otherwise shown to the reader (see jobs/daily.py / the daily formatters,
neither of which reference this module's state names)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from news_intel.pipeline.normalize import normalize_title

VERIFIED_FRESH = "verified_fresh"
VERIFIED_STALE = "verified_stale"
BOUNDED_UNVERIFIED = "bounded_unverified"
INSUFFICIENT_FRESHNESS_EVIDENCE = "insufficient_freshness_evidence"

# Same 48h buffer jobs/daily.py already uses for the verified-origin case
# (_STALE_ORIGIN_BUFFER) -- kept as one constant here so both the verified
# and bounded-unverified paths agree on what "recent" means.
STALE_BUFFER = timedelta(hours=48)
NEAR_DUPLICATE_TITLE_THRESHOLD = 88  # matches config/policy.yaml's dedupe.title_similarity_threshold


@dataclass(frozen=True)
class FreshnessAssessment:
    state: str
    reason: str

    @property
    def is_fresh(self) -> bool:
        return self.state in (VERIFIED_FRESH, BOUNDED_UNVERIFIED)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt


def _has_older_matching_history(
    conn: sqlite3.Connection,
    *,
    canonical_url: str | None,
    origin_url: str | None,
    normalized_title: str | None,
    origin_family: str | None,
    fetched_at: datetime,
    exclude_raw_item_id: int | None,
) -> bool:
    """True if stored history already has a raw_item for the same URL (or a
    near-duplicate title from the same origin_family) with a strictly
    earlier fetched_at -- direct, structural evidence this "new" discovery
    is really an old article resurfacing, not a heuristic over free text."""
    urls = [u for u in (canonical_url, origin_url) if u]
    if urls:
        placeholders = ",".join("?" for _ in urls)
        rows = conn.execute(
            f"SELECT id FROM raw_items WHERE fetched_at < ? "
            f"AND (canonical_url IN ({placeholders}) OR origin_url IN ({placeholders})) LIMIT 1",
            (fetched_at.isoformat(), *urls, *urls),
        ).fetchall()
        if rows and (exclude_raw_item_id is None or rows[0]["id"] != exclude_raw_item_id):
            return True

    if normalized_title and origin_family:
        candidates = conn.execute(
            "SELECT normalized_title FROM raw_items WHERE fetched_at < ? AND origin_family = ? "
            "ORDER BY fetched_at DESC LIMIT 50",
            (fetched_at.isoformat(), origin_family),
        ).fetchall()
        for row in candidates:
            if fuzz.ratio(normalized_title, row["normalized_title"]) >= NEAR_DUPLICATE_TITLE_THRESHOLD:
                return True

    return False


def assess_freshness(
    conn: sqlite3.Connection,
    item: dict,
    *,
    window_end: datetime,
    event_first_seen_at: datetime | None,
) -> FreshnessAssessment:
    """`item` is an event_repo.event_as_digest_dict()-shaped dict; its
    primary source carries origin_published_at/origin_verified/origin_url
    (from event_repo.event_sources) plus the raw fields needed for the
    history lookup below."""
    primary = item["sources"][0] if item.get("sources") else {}

    origin_published_at = _parse(primary.get("origin_published_at"))
    if origin_published_at is not None:
        if origin_published_at >= (window_end - STALE_BUFFER):
            return FreshnessAssessment(VERIFIED_FRESH, "origin_published_at verified and within the freshness buffer")
        return FreshnessAssessment(VERIFIED_STALE, "origin_published_at verified but older than the freshness buffer")

    # "Blocked origin" specifically means: an aggregator/citation-passthrough
    # source resolved a SPECIFIC origin_url (so this state machine's concern
    # -- an aggregator resurfacing an old origin article -- actually
    # applies) but could not fetch that origin's own publish time (paywall/
    # bot-check). An ordinary direct-publisher item that never had an
    # origin_url to begin with (the common case -- most sources never
    # attempt this fetch at all, see adapters/rss.py) is not a "blocked
    # origin" in this sense; its own published_at is already the genuine,
    # already-validated source time (pipeline/time_semantics.py's
    # published_anchor_at/archive_only), so it is fresh by construction and
    # never needs the stricter bounded_unverified gate below.
    if not primary.get("origin_url"):
        return FreshnessAssessment(VERIFIED_FRESH, "no aggregator origin resolution attempted for this source")

    # Blocked/unverifiable origin from here on -- bounded_unverified
    # requires every applicable safeguard to pass; anything missing or
    # failing is insufficient_freshness_evidence, never silently fresh.
    aggregator_published_at = _parse(primary.get("published_at"))
    if aggregator_published_at is None:
        return FreshnessAssessment(
            INSUFFICIENT_FRESHNESS_EVIDENCE, "no verified origin time and no aggregator published_at at all"
        )
    if aggregator_published_at < (window_end - STALE_BUFFER):
        return FreshnessAssessment(
            INSUFFICIENT_FRESHNESS_EVIDENCE, "aggregator published_at is itself older than the freshness buffer"
        )

    if event_first_seen_at is None or event_first_seen_at < (window_end - STALE_BUFFER):
        return FreshnessAssessment(
            INSUFFICIENT_FRESHNESS_EVIDENCE, "event first_seen_at is missing or older than the freshness buffer"
        )

    raw_item_id_str = str(primary.get("id") or "")
    raw_item_id_suffix = raw_item_id_str[1:]
    raw_item_id = (
        int(raw_item_id_suffix)
        if raw_item_id_str.startswith("S") and raw_item_id_suffix.isdigit()
        else None
    )
    fetched_at = _parse(primary.get("fetched_at")) or window_end
    if _has_older_matching_history(
        conn,
        canonical_url=primary.get("url"),
        origin_url=primary.get("origin_url"),
        normalized_title=normalize_title(primary.get("raw_title") or ""),
        origin_family=primary.get("origin_family"),
        fetched_at=fetched_at,
        exclude_raw_item_id=raw_item_id,
    ):
        return FreshnessAssessment(
            INSUFFICIENT_FRESHNESS_EVIDENCE,
            "an older raw_item with the same URL or a near-duplicate title/origin_family already exists in history "
            "-- treating this as the aggregator resurfacing an unchanged old article, not new news",
        )

    return FreshnessAssessment(
        BOUNDED_UNVERIFIED,
        "origin unverifiable (paywall/bot-check), but aggregator publication time, event first-seen time, and "
        "stored history all corroborate genuine recency",
    )
