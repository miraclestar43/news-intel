"""The prepared brief units (deploy/systemd/pi-news-brief*): Beijing-time calendars, a timeout that covers the
editorial budget, and a Telegram unit that only ever sends the frozen snapshot."""

from __future__ import annotations

import re
from pathlib import Path

from news_intel.jobs import brief

UNITS = Path(__file__).resolve().parents[2] / "deploy" / "systemd"


def _get(unit: str, key: str) -> list[str]:
    return re.findall(rf"^{key}=(.+)$", (UNITS / unit).read_text(), re.MULTILINE)


def test_build_timeout_covers_the_editorial_budget_with_headroom():
    timeout = int(_get("pi-news-brief.service", "TimeoutStartSec")[0])
    assert timeout >= brief.BUDGET_SECONDS + 600


def test_build_and_send_run_on_beijing_time_before_the_newsletter():
    cal = _get("pi-news-brief.timer", "OnCalendar")
    assert cal == ["*-*-* 20:05:00 Asia/Shanghai", "*-*-* 20:50:00 Asia/Shanghai"]
    assert _get("pi-news-brief-telegram.timer", "OnCalendar") == ["*-*-* 21:00:00 Asia/Shanghai"]
    assert _get("pi-news-brief.timer", "Persistent") == ["false"]


def test_the_telegram_unit_only_sends_the_frozen_brief():
    cmd = _get("pi-news-brief-telegram.service", "ExecStart")[0]
    assert "send-telegram --real" in cmd and "build" not in cmd
    assert _get("pi-news-brief.service", "ExecStart")[0].endswith("news_intel.jobs.brief build")
