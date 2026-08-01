from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, safe_get_with_retry


def _origin_family(feed_url: str, entry_link: str) -> str:
    # Two outlets syndicating the same wire copy should not count as
    # independently corroborating; group by the feed's own domain here,
    # refined later in pipeline.dedupe using entity/content overlap.
    return urlparse(feed_url).netloc or urlparse(entry_link).netloc


def _plain_text(html_or_text: str) -> str:
    # Some feeds (e.g. NVIDIA developer blog) put full HTML post bodies in
    # <description>; strip markup so stored excerpts and rendered digests
    # never leak raw <img>/<div> tags.
    return BeautifulSoup(html_or_text, "html.parser").get_text(separator=" ", strip=True)


@register("rss")
class RssAdapter:
    def fetch(self, source, cursor: str | None) -> FetchOutcome:
        try:
            result = safe_get_with_retry(source.url, timeout=source.fetch_timeout)
        except UnsafeURLError as exc:
            return FetchOutcome([], cursor, SourceHealthState.UNAVAILABLE, str(exc))
        except httpx.HTTPStatusError as exc:
            return FetchOutcome(
                [], cursor, SourceHealthState.DEGRADED, f"http {exc.response.status_code}"
            )
        except httpx.TransportError as exc:
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"transport error: {exc}")

        if result.not_modified:
            return FetchOutcome([], cursor, SourceHealthState.HEALTHY, "not modified")

        return self.parse(result.text, source, cursor)

    def parse(self, xml_text: str, source, cursor: str | None) -> FetchOutcome:
        """Split out from fetch() so fixture tests can exercise feed parsing
        without any network access."""
        parsed = feedparser.parse(xml_text)
        if parsed.bozo and not parsed.entries:
            return FetchOutcome(
                [], cursor, SourceHealthState.DEGRADED, f"feed parse error: {parsed.bozo_exception}"
            )

        items: list[CollectedItem] = []
        origin_family = _origin_family(source.url, "")
        for entry in parsed.entries:
            link = entry.get("link", "")
            title = entry.get("title", "").strip()
            if not title or not link:
                continue
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if entry.get(key):
                    published = datetime(*entry[key][:6], tzinfo=UTC)
                    break
            raw_summary = (entry.get("summary") or entry.get("description") or "").strip()
            summary = _plain_text(raw_summary) if raw_summary else ""
            external_id = entry.get("id") or link
            items.append(
                CollectedItem(
                    source_id=source.id,
                    external_id=external_id,
                    canonical_url=link,
                    title=title,
                    summary_excerpt=summary[: source.excerpt_max_chars] if summary else None,
                    language=source.default_language,
                    published_at=published,
                    origin_family=origin_family,
                    metadata={"source_name": source.name},
                )
            )

        new_cursor = hashlib.sha256(xml_text.encode("utf-8", "ignore")).hexdigest()
        return FetchOutcome(items, new_cursor, SourceHealthState.HEALTHY)
