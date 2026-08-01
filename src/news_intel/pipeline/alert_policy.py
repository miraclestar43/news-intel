"""Pure quiet-hours time arithmetic (design doc section 3.3), kept free of
DB access so it's trivially unit-testable across the day boundary."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from news_intel.settings import QuietHours


def _parse_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def is_quiet_hours(now_time: time, quiet_hours: QuietHours) -> bool:
    start = _parse_time(quiet_hours.start)
    end = _parse_time(quiet_hours.end)
    if start <= end:
        return start <= now_time < end
    return now_time >= start or now_time < end  # wraps midnight, e.g. 23:30-06:30


def next_quiet_hours_end(now_local: datetime, quiet_hours: QuietHours) -> datetime:
    """The next moment quiet hours release (06:30 by default), given `now_local`
    is already known to be inside quiet hours."""
    end = _parse_time(quiet_hours.end)
    candidate = now_local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if now_local.time() < end:
        return candidate
    return candidate + timedelta(days=1)


def pt_calendar_date(dt_utc_or_local: datetime, tz) -> date:
    return dt_utc_or_local.astimezone(tz).date()
