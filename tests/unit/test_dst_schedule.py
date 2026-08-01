"""Acceptance test 8: 10 PM Pacific behavior must be correct across both the
March (spring-forward) and November (fall-back) DST transitions. All
schedules use the IANA zone America/Los_Angeles, never a fixed UTC offset
(design doc section 3), so this test locks in that the runtime's tzdata
produces the right UTC offset on both sides of each 2026 transition."""

from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def _offset_hours(dt: datetime) -> float:
    return dt.utcoffset().total_seconds() / 3600.0


def test_pst_before_spring_forward_2026():
    # 2026 spring-forward is Sunday March 8 at 02:00 local.
    dt = datetime(2026, 3, 7, 22, 0, tzinfo=PT)
    assert _offset_hours(dt) == -8.0


def test_pdt_after_spring_forward_2026():
    dt = datetime(2026, 3, 9, 22, 0, tzinfo=PT)
    assert _offset_hours(dt) == -7.0


def test_pdt_before_fall_back_2026():
    # 2026 fall-back is Sunday November 1 at 02:00 local.
    dt = datetime(2026, 10, 31, 22, 0, tzinfo=PT)
    assert _offset_hours(dt) == -7.0


def test_pst_after_fall_back_2026():
    dt = datetime(2026, 11, 2, 22, 0, tzinfo=PT)
    assert _offset_hours(dt) == -8.0


def test_daily_job_date_label_stable_across_dst(config, conn):
    from news_intel.jobs.daily import run_daily

    before = run_daily(config, conn, dry_run=True, now=datetime(2026, 3, 7, 22, 0, tzinfo=PT))
    conn.execute("DELETE FROM digests")
    conn.execute("DELETE FROM job_locks")
    after = run_daily(config, conn, dry_run=True, now=datetime(2026, 3, 9, 22, 0, tzinfo=PT))

    assert "Mar 7" in before.rendered_text
    assert "Mar 9" in after.rendered_text
