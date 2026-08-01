from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, safe_get_with_retry


@register("json_federal_register")
class FederalRegisterAdapter:
    """https://www.federalregister.gov/api/v1/documents.json — no API key required."""

    def fetch(self, source, cursor: str | None) -> FetchOutcome:
        params = "?per_page=40&order=newest&fields[]=document_number&fields[]=title" \
                  "&fields[]=html_url&fields[]=publication_date&fields[]=agencies" \
                  "&fields[]=abstract&fields[]=type"
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
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"invalid json: {exc}")

        items: list[CollectedItem] = []
        first_doc_number: str | None = None
        for doc in payload.get("results", []):
            doc_number = doc.get("document_number")
            if not doc_number:
                continue
            if first_doc_number is None:
                first_doc_number = doc_number
            if cursor is not None and doc_number == cursor:
                break
            pub_date = doc.get("publication_date")
            published = None
            if pub_date:
                published = datetime.strptime(pub_date, "%Y-%m-%d").replace(tzinfo=UTC)
            agencies = ", ".join(a.get("name", "") for a in doc.get("agencies", []))
            items.append(
                CollectedItem(
                    source_id=source.id,
                    external_id=doc_number,
                    canonical_url=doc.get("html_url", ""),
                    title=doc.get("title", ""),
                    summary_excerpt=(doc.get("abstract") or "")[: source.excerpt_max_chars],
                    language="en",
                    published_at=published,
                    origin_family="federalregister.gov",
                    metadata={"agencies": agencies, "doc_type": doc.get("type")},
                )
            )

        return FetchOutcome(items, first_doc_number or cursor, SourceHealthState.HEALTHY)
