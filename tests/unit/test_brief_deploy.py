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
    assert not (UNITS / "pi-news-brief-telegram.timer").exists()  # the send follows the build, not a clock
    assert _get("pi-news-brief.timer", "Persistent") == ["false"]


def test_the_telegram_unit_only_sends_the_frozen_brief():
    cmd = _get("pi-news-brief-telegram.service", "ExecStart")[0]
    assert "send-telegram --real" in cmd and "build" not in cmd
    assert _get("pi-news-brief.service", "ExecStart")[0].endswith("news_intel.jobs.brief build")


def test_the_send_is_started_by_a_successful_build_including_a_degraded_one():
    dropin = (UNITS / "pi-news-brief.service.d" / "send-telegram.conf").read_text()
    assert re.search(r"^OnSuccess=pi-news-brief-telegram\.service$", dropin, re.MULTILINE)
    assert re.search(r"^SuccessExitStatus=3$", dropin, re.MULTILINE)  # built_degraded publishes, so it is sent
    assert "75" not in re.findall(r"^SuccessExitStatus=(.*)$", dropin, re.MULTILINE)[0]  # retry_later never sends
