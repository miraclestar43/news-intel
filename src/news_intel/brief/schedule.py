"""Edition date, cutoff and window, on Asia/Shanghai time (2026-11-01).

The newsletter goes out at 22:08 Beijing time. The brief's cutoff is 20:00 Beijing time on the edition date and it
covers the 24 hours before it, so it is built and frozen well before the send. Asia/Shanghai has no DST; the cutoff
is the same UTC instant all year (12:00 UTC), whatever the US clock does (America/Los_Angeles moved 2026-11-01).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
CUTOFF_LOCAL = time(20, 0)
WINDOW_HOURS = 24


@dataclass(frozen=True)
class Edition:
    edition_date: date  # Beijing calendar date
    cutoff: datetime  # aware, Asia/Shanghai
    window_start: datetime

    @property
    def cutoff_utc(self) -> datetime:
        return self.cutoff.astimezone(UTC)

    def label_zh(self) -> str:
        c = self.cutoff
        return f"截至北京时间{c.month}月{c.day}日{c:%H:%M} · 过去{WINDOW_HOURS}小时要闻"


def edition_for(edition_date: date, cutoff_local: time = CUTOFF_LOCAL) -> Edition:
    cutoff = datetime.combine(edition_date, cutoff_local, tzinfo=BEIJING)
    return Edition(edition_date, cutoff, cutoff - timedelta(hours=WINDOW_HOURS))


def edition_at(cutoff: datetime) -> Edition:
    """An edition with an explicit cutoff instant (replays, or a run before the regular cutoff)."""
    local = cutoff.astimezone(BEIJING)
    return Edition(local.date(), local, local - timedelta(hours=WINDOW_HOURS))


def current_edition(now: datetime) -> Edition:
    """The edition a run at `now` builds: today's (Beijing) once its cutoff has passed, else yesterday's."""
    local = now.astimezone(BEIJING)
    today = edition_for(local.date())
    return today if local >= today.cutoff else edition_for(local.date() - timedelta(days=1))
