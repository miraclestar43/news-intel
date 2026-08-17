from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, retry_after_seconds, safe_get_with_retry

# 2026-08-17 zh_cn source-strategy repair: every html_index item previously
# hardcoded published_at=None regardless of config -- pipeline/
# time_semantics.resolve_anchor then made every such item archive_only=True,
# permanently ineligible for daily selection no matter how material. A
# `date` selector was already accepted in config/sources.yaml (gov_cn_policy
# has one) but never actually read. This adds real extraction, general
# across both the ISO-style dates most English listing pages use and the
# Chinese-locale formats government pages commonly use
# ("2026年8月17日"/"2026-08-17"/"08-17" with no year, verified live against
# gov.cn's own listing markup).
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)")


def _parse_index_date(text: str, *, now: datetime | None = None) -> datetime | None:
    """Best-effort, conservative date parsing from a listing row's own date
    cell -- never invents a date it can't find; returns None (not today's
    date, not a guess) when the text doesn't match a recognized pattern."""
    if not text:
        return None
    text = text.strip()
    for pattern in (_ISO_DATE_RE, _CN_DATE_RE):
        m = pattern.search(text)
        if m:
            year, month, day = (int(g) for g in m.groups())
            try:
                return datetime(year, month, day, tzinfo=UTC)
            except ValueError:
                continue
    m = _SHORT_DATE_RE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = (now or datetime.now(UTC)).year
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            return None
    return None


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
                [], cursor, SourceHealthState.DEGRADED, f"http {exc.response.status_code}",
                retry_after_seconds=retry_after_seconds(exc) if exc.response.status_code == 429 else None,
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
        date_sel = selectors.get("date")

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
            published_at = None
            if date_sel:
                date_el = row.select_one(date_sel)
                if date_el is not None:
                    published_at = _parse_index_date(date_el.get_text(strip=True))
            items.append(
                CollectedItem(
                    source_id=source.id,
                    external_id=absolute_url,
                    canonical_url=absolute_url,
                    title=title,
                    summary_excerpt=None,
                    language=language,
                    published_at=published_at,
                    origin_family=source.id,
                    metadata={"source_name": source.name},
                )
            )

        new_cursor = hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()
        return FetchOutcome(items, new_cursor, SourceHealthState.HEALTHY)
