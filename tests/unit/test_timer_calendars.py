"""2026-08-29: regression tests for the installed systemd timer calendars.

The zh_cn daily timer shipped as "Mon..Sat", i.e. six days a week rather
than daily, while en_global ran every day. The Sunday policy (the weekly
brief replaces the daily send) is already enforced in the application for
BOTH editions -- jobs/daily.py returns skipped_sunday=True on
pt_weekday == 6 -- so encoding it again in one edition's calendar made the
two cadences silently differ."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SYSTEMD_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"


def _oncalendar(unit_name: str) -> str:
    text = (SYSTEMD_DIR / unit_name).read_text(encoding="utf-8")
    values = [
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("OnCalendar=")
    ]
    assert len(values) == 1, f"{unit_name}: expected exactly one OnCalendar, got {values}"
    return values[0]


def test_zh_cn_daily_calendar_is_every_calendar_day():
    expr = _oncalendar("pi-news-daily-zh.timer")
    assert not re.search(r"Mon|Tue|Wed|Thu|Fri|Sat|Sun", expr), (
        f"zh_cn daily calendar restricts weekdays and is not daily: {expr!r}"
    )
    assert expr == "*-*-* 07:00:00 America/Los_Angeles"


def test_en_global_daily_calendar_is_every_calendar_day():
    expr = _oncalendar("pi-news-daily.timer")
    assert not re.search(r"Mon|Tue|Wed|Thu|Fri|Sat|Sun", expr), (
        f"en_global daily calendar restricts weekdays and is not daily: {expr!r}"
    )


def test_both_daily_timers_fire_at_most_once_per_day():
    for unit in ("pi-news-daily.timer", "pi-news-daily-zh.timer"):
        expr = _oncalendar(unit)
        # a single HH:MM:SS with no repetition/range syntax in the time field
        time_field = expr.split()[-2] if expr.endswith(("America/Los_Angeles", "UTC")) else expr.split()[-1]
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", time_field), f"{unit}: {expr!r}"


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze unavailable")
def test_systemd_agrees_both_calendars_are_daily():
    """Ask systemd itself: 7 consecutive iterations must span 7 distinct
    calendar days with no gap -- which "Mon..Sat" would fail on Sunday."""
    for unit in ("pi-news-daily.timer", "pi-news-daily-zh.timer"):
        expr = _oncalendar(unit)
        out = subprocess.run(
            ["systemd-analyze", "calendar", "--iterations=7", expr],
            capture_output=True, text=True, check=True,
        ).stdout
        dates = re.findall(r"\(in UTC\): \w{3} (\d{4}-\d{2}-\d{2})", out)
        assert len(dates) == 7, f"{unit}: could not read 7 iterations from {out!r}"
        from datetime import date, timedelta

        parsed = [date.fromisoformat(d) for d in dates]
        for earlier, later in zip(parsed, parsed[1:], strict=False):
            assert later - earlier == timedelta(days=1), (
                f"{unit} ({expr!r}) skips a calendar day: {earlier} -> {later}"
            )
