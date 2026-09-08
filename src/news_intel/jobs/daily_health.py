"""Edition-scoped daily-delivery health reconstruction (2026-09-07).

Two production incidents in a row were only noticed because the USER
noticed Telegram had gone quiet:

  - Aug 26-29: en_global fail-closed on message length every night; the
    early return happened before record_digest(), so the window cursor
    froze and each night's digest grew until it could never send.
  - Sep 6-7:  run_daily() stood aside for a weekly/monthly brief that was
    disabled, so nothing was produced at all -- and because the skip
    returned before job_runs.start(), it left NO run record whatsoever
    while systemd logged "success" in about one second.

In both cases the operator-visible signals (systemd result, journal) said
everything was fine. This module reconstructs, per edition per expected
Pacific date, what actually reached the reader -- and distinguishes the
outcomes that all previously looked identical from the outside:

    delivered              a message was sent
    delivered_multipart    sent, split across >1 Telegram message
    zero_qualified         ran, genuinely found nothing to report
    degraded_collection    ran, but the collector barely fed it
    render_failed          selected stories but produced no message
    delivery_failed        rendered a message but the send did not land
    never_ran              the timer/service left no trace at all

`never_ran` is the one that matters most: it is the signature both
incidents shared, and the one nothing else in the system reported.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# A collector this quiet over a digest window means the window was starved,
# not that the world was quiet -- healthy production is ~288 runs/day.
_DEGRADED_COLLECT_RUNS = 12
_DEGRADED_FETCH_ITEMS = 100


@dataclass
class EditionDayHealth:
    edition_id: str
    pt_date: date
    status: str
    detail: str
    digest_id: int | None = None
    rendered_chars: int = 0
    parts: int = 0
    delivered: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("delivered", "delivered_multipart", "zero_qualified")


def _expected_pt_dates(days: int, today_pt: date) -> list[date]:
    return [today_pt - timedelta(days=n) for n in range(days - 1, -1, -1)]


def _collector_health(conn: sqlite3.Connection, start: datetime, end: datetime) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT detail_json FROM job_runs WHERE job_name='collect' "
        "AND started_at >= ? AND started_at < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    fetched = 0
    for r in rows:
        try:
            fetched += json.loads(r["detail_json"] or "{}").get("fetched_items", 0)
        except (json.JSONDecodeError, TypeError):
            continue
    return len(rows), fetched


def edition_day_health(
    conn: sqlite3.Connection,
    *,
    edition_id: str,
    pt_date: date,
    timezone: str = "America/Los_Angeles",
    max_chars: int = 4096,
) -> EditionDayHealth:
    """Reconstruct what the reader actually received for one edition/date."""
    tz = ZoneInfo(timezone)
    day_start = datetime.combine(pt_date, datetime.min.time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    row = conn.execute(
        "SELECT id, rendered_text, window_start, window_end FROM digests "
        "WHERE kind='daily' AND edition_id = ? AND created_at >= ? AND created_at < ? "
        "AND rendered_text != '' ORDER BY created_at DESC LIMIT 1",
        (edition_id, day_start.astimezone(ZoneInfo("UTC")).isoformat(),
         day_end.astimezone(ZoneInfo("UTC")).isoformat()),
    ).fetchone()

    if row is None:
        runs, fetched = _collector_health(
            conn, day_start.astimezone(ZoneInfo("UTC")), day_end.astimezone(ZoneInfo("UTC"))
        )
        if runs < _DEGRADED_COLLECT_RUNS or fetched < _DEGRADED_FETCH_ITEMS:
            return EditionDayHealth(
                edition_id, pt_date, "degraded_collection",
                f"no digest recorded; collector ran {runs} times fetching {fetched} items",
            )
        return EditionDayHealth(
            edition_id, pt_date, "never_ran",
            f"no digest row at all, though the collector was healthy "
            f"({runs} runs, {fetched} items) -- the job never produced a digest",
        )

    from news_intel.delivery.splitter import split_and_label

    text = row["rendered_text"] or ""
    parts = len(split_and_label(text, max_chars)) if text else 0

    events = conn.execute(
        "SELECT COUNT(*) AS n FROM digest_events WHERE digest_id = ?", (row["id"],)
    ).fetchone()["n"]

    delivered = conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE status='sent' AND created_at >= ? AND created_at < ?",
        (day_start.astimezone(ZoneInfo("UTC")).isoformat(),
         day_end.astimezone(ZoneInfo("UTC")).isoformat()),
    ).fetchone()["n"] > 0

    if events == 0:
        return EditionDayHealth(
            edition_id, pt_date, "zero_qualified",
            "ran and rendered, but no story qualified for this window",
            row["id"], len(text), parts, delivered,
        )
    if not text.strip():
        return EditionDayHealth(
            edition_id, pt_date, "render_failed",
            f"{events} stories selected but the rendered message was empty",
            row["id"], 0, 0, delivered,
        )
    if not delivered:
        return EditionDayHealth(
            edition_id, pt_date, "delivery_failed",
            f"rendered {len(text)} chars ({parts} part(s)) but no successful delivery was recorded",
            row["id"], len(text), parts, False,
        )
    status = "delivered_multipart" if parts > 1 else "delivered"
    return EditionDayHealth(
        edition_id, pt_date, status,
        f"{events} stories, {len(text)} chars, {parts} Telegram message(s)",
        row["id"], len(text), parts, True,
    )


def daily_health_report(
    conn: sqlite3.Connection, config, *, days: int = 7, now: datetime | None = None
) -> list[EditionDayHealth]:
    now = now or datetime.now(ZoneInfo("UTC"))
    results: list[EditionDayHealth] = []
    for edition in config.editions.editions:
        tz = edition.schedule.timezone
        today_pt = now.astimezone(ZoneInfo(tz)).date()
        for pt_date in _expected_pt_dates(days, today_pt):
            results.append(
                edition_day_health(
                    conn, edition_id=edition.id, pt_date=pt_date, timezone=tz,
                    max_chars=config.policy.digests.telegram_hard_limit_chars,
                )
            )
    return results
