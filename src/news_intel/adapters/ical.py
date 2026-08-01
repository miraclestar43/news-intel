from __future__ import annotations

import hashlib
from datetime import UTC, datetime, time

import httpx
from icalendar import Calendar

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState, register
from news_intel.net import UnsafeURLError, safe_get_with_retry


@register("ical")
class IcalAdapter:
    """BLS/BEA release calendars — feeds scheduled_events, not events/alerts."""

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

    def parse(self, ics_text: str, source, cursor: str | None) -> FetchOutcome:
        """Split out from fetch() so fixture tests can exercise parsing
        without any network access."""
        try:
            cal = Calendar.from_ical(ics_text)
        except ValueError as exc:
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"invalid ics: {exc}")

        items: list[CollectedItem] = []
        for component in cal.walk("VEVENT"):
            summary = str(component.get("summary", "")).strip()
            if not summary:
                continue
            dtstart = component.get("dtstart")
            when = dtstart.dt if dtstart else None
            if isinstance(when, datetime):
                published = when if when.tzinfo else when.replace(tzinfo=UTC)
            elif when is not None:
                published = datetime.combine(when, time.min, tzinfo=UTC)
            else:
                published = None
            uid = str(component.get("uid", "")) or hashlib.sha256(
                summary.encode()
            ).hexdigest()
            items.append(
                CollectedItem(
                    source_id=source.id,
                    external_id=uid,
                    canonical_url=str(component.get("url", "")) or source.url,
                    title=summary,
                    summary_excerpt=str(component.get("description", ""))[
                        : source.excerpt_max_chars
                    ]
                    or None,
                    language="en",
                    published_at=published,
                    origin_family="scheduled_calendar",
                    metadata={"is_calendar_event": True},
                )
            )

        new_cursor = hashlib.sha256(ics_text.encode("utf-8", "ignore")).hexdigest()
        return FetchOutcome(items, new_cursor, SourceHealthState.HEALTHY)
