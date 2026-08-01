from __future__ import annotations

import hashlib
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, safe_get_with_retry


@register("html_index")
class HtmlIndexAdapter:
    """Configuration-driven index-page scraper (e.g. White House, BIS, gov.cn).

    Selectors live in config/sources.yaml, never hard-coded, so a page redesign
    degrades only that one source and gets a fixture test to catch it.
    """

    def fetch(self, source, cursor: str | None) -> FetchOutcome:
        selectors = source.selectors or {}
        item_sel = selectors.get("item")
        if not item_sel:
            return FetchOutcome(
                [], cursor, SourceHealthState.DISABLED, "no 'item' selector configured"
            )

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

        return self.parse(result.text, source, cursor)

    def parse(self, html: str, source, cursor: str | None) -> FetchOutcome:
        """Split out so fixture tests can exercise selector logic without network."""
        selectors = source.selectors or {}
        item_sel = selectors["item"]
        title_sel = selectors.get("title", "a")
        link_sel = selectors.get("link", "a")

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select(item_sel)
        if not rows:
            return FetchOutcome(
                [], cursor, SourceHealthState.DEGRADED, f"selector {item_sel!r} matched nothing"
            )

        items: list[CollectedItem] = []
        for row in rows:
            title_el = row.select_one(title_sel)
            link_el = row.select_one(link_sel)
            if title_el is None or link_el is None:
                continue
            title = title_el.get_text(strip=True)
            href = link_el.get("href", "")
            if not title or not href:
                continue
            absolute_url = urljoin(source.url, href)
            language = "zh" if any("一" <= c <= "鿿" for c in title) else "en"
            items.append(
                CollectedItem(
                    source_id=source.id,
                    external_id=absolute_url,
                    canonical_url=absolute_url,
                    title=title,
                    summary_excerpt=None,
                    language=language,
                    published_at=None,
                    origin_family=source.id,
                    metadata={"source_name": source.name},
                )
            )

        new_cursor = hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()
        return FetchOutcome(items, new_cursor, SourceHealthState.HEALTHY)
