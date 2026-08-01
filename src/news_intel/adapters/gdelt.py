from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, safe_get_with_retry

# T3 discovery/context only — never a core dependency, never independently
# confirms an event on its own.
_DEFAULT_QUERY = (
    '("AI chip" OR semiconductor OR "export control" OR tariff OR Fed OR '
    "Federal Reserve OR sanctions) sourcelang:english"
)


@register("gdelt")
class GdeltAdapter:
    def fetch(self, source, cursor: str | None) -> FetchOutcome:
        query = getattr(source, "query", None) or _DEFAULT_QUERY
        params = (
            f"?query={quote(query)}"
            "&mode=ArtList&maxrecords=50&format=json&sort=DateDesc"
        )
        url = source.url + params
        try:
            result = safe_get_with_retry(url, timeout=source.fetch_timeout)
        except UnsafeURLError as exc:
            return FetchOutcome([], cursor, SourceHealthState.UNAVAILABLE, str(exc))
        except httpx.HTTPStatusError as exc:
            return FetchOutcome(
                [], cursor, SourceHealthState.DEGRADED, f"http {exc.response.status_code}"
            )
        except httpx.TransportError as exc:
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"transport error: {exc}")

        return self.parse(result.text, source, cursor)

    def parse(self, json_text: str, source, cursor: str | None) -> FetchOutcome:
        """Split out from fetch() so fixture tests can exercise parsing
        without any network access."""
        try:
            payload = json.loads(json_text) if json_text.strip() else {"articles": []}
        except json.JSONDecodeError as exc:
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"invalid json: {exc}")

        items: list[CollectedItem] = []
        for art in payload.get("articles", []):
            url_ = art.get("url")
            title = art.get("title")
            if not url_ or not title:
                continue
            seendate = art.get("seendate")
            published = None
            if seendate:
                try:
                    published = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                except ValueError:
                    published = None
            domain = art.get("domain", "")
            items.append(
                CollectedItem(
                    source_id=source.id,
                    external_id=url_,
                    canonical_url=url_,
                    title=title,
                    summary_excerpt=None,
                    language=(art.get("language") or "en")[:2].lower(),
                    published_at=published,
                    origin_family=domain or "gdelt",
                    metadata={"gdelt_domain": domain, "tone": art.get("tone")},
                )
            )

        return FetchOutcome(items, cursor, SourceHealthState.HEALTHY)
