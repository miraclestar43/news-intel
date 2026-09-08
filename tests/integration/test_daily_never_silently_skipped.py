"""2026-09-07 production incident: no digest reached the user for Sep 6 or
Sep 7 in EITHER edition, while systemd recorded "success" for all four runs
in about one second each.

run_daily() stood aside unconditionally on Sunday ("the weekly brief
replaces the daily send") and on the first Monday of the month ("the
monthly brief replaces it"). Both the weekly and monthly timers are
DISABLED for this release, so nothing took the slot -- the digest was
simply never produced. Worse, the skip returns BEFORE job_runs.start() and
record_digest(), so it left no run record at all and froze both window
cursors (en_global 48.3h stale, zh_cn 63.3h stale by the time it was
noticed).

Sep 6 2026 was a Sunday; Sep 7 2026 was the first Monday of September.
Collection was healthy throughout (288 runs/day, ~167k items/day, zero
failures, 219-255 new en_global and 139-252 new zh_cn events on those two
days) -- there was abundant material, the job just never looked at it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from news_intel.jobs.daily import run_daily

# The exact instants the production timers fired, in UTC.
SEP6_ZH = datetime(2026, 9, 6, 14, 0, tzinfo=UTC)   # PT Sun Sep 6 07:00
SEP6_EN = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)    # PT Sun Sep 6 22:00
SEP7_ZH = datetime(2026, 9, 7, 14, 0, tzinfo=UTC)   # PT Mon Sep 7 07:00 (first Monday)
SEP7_EN = datetime(2026, 9, 8, 5, 0, tzinfo=UTC)    # PT Mon Sep 7 22:00 (first Monday)


@pytest.mark.parametrize(
    ("label", "when", "edition"),
    [
        ("Sep 6 zh_cn (Sunday)", SEP6_ZH, "zh_cn"),
        ("Sep 6 en_global (Sunday)", SEP6_EN, "en_global"),
        ("Sep 7 zh_cn (first Monday)", SEP7_ZH, "zh_cn"),
        ("Sep 7 en_global (first Monday)", SEP7_EN, "en_global"),
    ],
)
def test_the_four_missed_runs_are_no_longer_skipped(config, conn, label, when, edition):
    """With weekly/monthly disabled (the shipped default), none of the four
    runs that silently produced nothing may stand aside."""
    summary = run_daily(config, conn, chat_id=None, dry_run=True, now=when, edition_id=edition)
    assert summary.skipped_sunday is False, f"{label} still skipped for Sunday"
    assert summary.skipped_monthly_slot is False, f"{label} still skipped for the monthly slot"


def test_default_policy_is_not_to_stand_aside(config):
    """The shipped default must match what is actually deployed: weekly and
    monthly are disabled, so daily owns every calendar day."""
    for edition in config.editions.editions:
        assert edition.schedule.weekly_replaces_daily is False, edition.id
        assert edition.schedule.monthly_replaces_daily is False, edition.id


def test_daily_still_stands_aside_when_weekly_is_actually_enabled(config, conn):
    """The deferral is still available -- it is now conditional, not gone.
    Re-enabling the weekly brief must restore the no-double-send guard."""
    for edition in config.editions.editions:
        edition.schedule.weekly_replaces_daily = True
    summary = run_daily(config, conn, chat_id=None, dry_run=True, now=SEP6_EN, edition_id="en_global")
    assert summary.skipped_sunday is True


def test_daily_still_stands_aside_when_monthly_is_actually_enabled(config, conn):
    for edition in config.editions.editions:
        edition.schedule.monthly_replaces_daily = True
    summary = run_daily(config, conn, chat_id=None, dry_run=True, now=SEP7_EN, edition_id="en_global")
    assert summary.skipped_monthly_slot is True


def test_a_skipped_run_is_distinguishable_from_a_zero_story_run(config, conn):
    """The incident was invisible partly because a self-skip and a genuine
    empty day both surfaced as top3=0/sent=False. They must be tellable
    apart by the caller."""
    for edition in config.editions.editions:
        edition.schedule.weekly_replaces_daily = True
    skipped = run_daily(config, conn, chat_id=None, dry_run=True, now=SEP6_EN, edition_id="en_global")
    for edition in config.editions.editions:
        edition.schedule.weekly_replaces_daily = False
    empty = run_daily(config, conn, chat_id=None, dry_run=True, now=SEP6_EN, edition_id="en_global")

    assert skipped.skipped_sunday is True
    assert empty.skipped_sunday is False
    # a real (if empty) run renders a reader-facing message; a skip does not
    assert empty.rendered_text != ""
    assert skipped.rendered_text == ""
