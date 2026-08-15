from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, retry_after_seconds, safe_get_with_retry
from news_intel.pipeline.citation import resolve_citation_publisher
from news_intel.pipeline.publisher_family import registrable_domain


def _origin_family(feed_url: str, entry_link: str) -> str:
    # Two outlets syndicating the same wire copy should not count as
    # independently corroborating; group by the feed's own domain here,
    # refined later in pipeline.dedupe using entity/content overlap.
    return urlparse(feed_url).netloc or urlparse(entry_link).netloc


def _extract_origin_url(feed_domain: str, raw_description_html: str) -> str | None:
    """The verified original-article URL for an aggregator-style entry
    (2026-08-15 publisher-family repair). Only meaningful for feeds whose
    own entry link is an aggregator roundup page, not the original article
    (Techmeme) -- gated by the caller on a detected citation prefix so this
    never fires on an ordinary single-publisher feed, where a stray
    outbound link in the description (an image credit, a "read more" link
    to an unrelated domain) could otherwise be mistaken for the origin.

    Verified against a live Techmeme feed sample: each entry's description
    opens with an <A HREF="..."> to the original article (wrapping its
    thumbnail image), consistently ahead of Techmeme's own "Techmeme
    permalink" anchor and matching the headline's own link later in the
    same description -- so the first outbound (different-registrable-
    domain) anchor is a reliable signal, not a guess."""
    soup = BeautifulSoup(raw_description_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        if registrable_domain(parsed.netloc) == feed_domain:
            continue
        return href
    return None


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
                [], cursor, SourceHealthState.DEGRADED, f"http {exc.response.status_code}",
                retry_after_seconds=retry_after_seconds(exc) if exc.response.status_code == 429 else None,
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
        feed_domain = registrable_domain(urlparse(source.url).netloc)
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
            # Only an aggregator-style entry (detected by its own citation
            # prefix, e.g. Techmeme's "Author / Publication: ...") gets an
            # origin_url extraction attempt -- see _extract_origin_url.
            origin_url = (
                _extract_origin_url(feed_domain, raw_summary)
                if raw_summary and resolve_citation_publisher(summary)
                else None
            )
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
                    origin_url=origin_url,
                    metadata={"source_name": source.name},
                )
            )

        new_cursor = hashlib.sha256(xml_text.encode("utf-8", "ignore")).hexdigest()
        return FetchOutcome(items, new_cursor, SourceHealthState.HEALTHY)
