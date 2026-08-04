from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, retry_after_seconds, safe_get_with_retry

# T3 discovery only: HN popularity never independently confirms an event.
_MAX_STORIES_PER_POLL = 25


def story_to_item(story: dict, story_id: int, source) -> CollectedItem | None:
    """Pure conversion, fixture-testable without network: a raw HN /item/
    payload becomes a CollectedItem, or None if it isn't a linked story."""
    if not story or story.get("type") != "story" or not story.get("url"):
        return None
    return CollectedItem(
        source_id=source.id,
        external_id=str(story_id),
        canonical_url=story["url"],
        title=story.get("title", ""),
        summary_excerpt=None,
        language="en",
        published_at=datetime.fromtimestamp(story["time"], tz=UTC) if story.get("time") else None,
        origin_family="news.ycombinator.com",
        metadata={"hn_score": story.get("score", 0), "hn_id": story_id},
    )


@register("json_hn")
class HackerNewsAdapter:
    def fetch(self, source, cursor: str | None) -> FetchOutcome:
        base = source.url.rstrip("/")
        try:
            top_result = safe_get_with_retry(f"{base}/topstories.json", timeout=source.fetch_timeout)
        except UnsafeURLError as exc:
            return FetchOutcome([], cursor, SourceHealthState.UNAVAILABLE, str(exc))
        except httpx.HTTPStatusError as exc:
            return FetchOutcome(
                [], cursor, SourceHealthState.DEGRADED, f"http {exc.response.status_code}",
                retry_after_seconds=retry_after_seconds(exc) if exc.response.status_code == 429 else None,
            )
        except httpx.TransportError as exc:
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"transport error: {exc}")

        try:
            story_ids = json.loads(top_result.text)[:_MAX_STORIES_PER_POLL]
        except (json.JSONDecodeError, TypeError) as exc:
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"invalid json: {exc}")

        seen_cutoff = int(cursor) if cursor else 0
        items: list[CollectedItem] = []
        max_id = seen_cutoff
        failures = 0
        for story_id in story_ids:
            max_id = max(max_id, story_id)
            if story_id <= seen_cutoff:
                continue
            try:
                item_result = safe_get_with_retry(
                    f"{base}/item/{story_id}.json", timeout=source.fetch_timeout
                )
                story = json.loads(item_result.text)
            except (UnsafeURLError, httpx.HTTPError, json.JSONDecodeError):
                failures += 1
                continue
            item = story_to_item(story, story_id, source)
            if item is not None:
                items.append(item)

        health = SourceHealthState.HEALTHY
        detail = None
        if failures and failures == len(story_ids):
            health = SourceHealthState.DEGRADED
            detail = f"{failures} item lookups failed"
        elif failures:
            detail = f"{failures} item lookups failed (partial)"

        return FetchOutcome(items, str(max_id), health, detail)
