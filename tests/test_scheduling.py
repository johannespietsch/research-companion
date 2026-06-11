"""Unit tests for the daily-loop scheduling helper shared by the scan + prune
loops in main.py."""
from datetime import datetime, timezone

from bot.scheduling import next_daily_run


def _utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def test_slot_later_today_is_used():
    # 01:00, target 04:00 → same day 04:00.
    assert next_daily_run(_utc(2026, 6, 10, 1), 4) == _utc(2026, 6, 10, 4)


def test_slot_already_passed_rolls_to_tomorrow():
    # 05:00, target 04:00 → next day 04:00.
    assert next_daily_run(_utc(2026, 6, 10, 5), 4) == _utc(2026, 6, 11, 4)


def test_exactly_on_the_slot_rolls_to_tomorrow():
    # Exactly 04:00 counts as passed, so we don't fire twice in one tick.
    assert next_daily_run(_utc(2026, 6, 10, 4), 4) == _utc(2026, 6, 11, 4)


def test_result_is_always_in_the_future():
    now = _utc(2026, 6, 10, 4, 0, 1)
    assert next_daily_run(now, 4) > now


def test_crosses_month_boundary():
    assert next_daily_run(_utc(2026, 6, 30, 23), 4) == _utc(2026, 7, 1, 4)


# --- next_weekly_run (weekly digest) ---------------------------------------

from bot.scheduling import next_weekly_run  # noqa: E402

FRIDAY = 4


def test_weekly_slot_later_this_week_is_used():
    # Wed 10 Jun 2026 → Fri 12 Jun 06:00.
    assert next_weekly_run(_utc(2026, 6, 10, 1), FRIDAY, 6) == _utc(2026, 6, 12, 6)


def test_weekly_slot_same_day_before_hour_is_used():
    # Fri 12 Jun 05:00 → Fri 12 Jun 06:00.
    assert next_weekly_run(_utc(2026, 6, 12, 5), FRIDAY, 6) == _utc(2026, 6, 12, 6)


def test_weekly_slot_passed_rolls_a_full_week():
    # Fri 12 Jun 07:00 → Fri 19 Jun 06:00.
    assert next_weekly_run(_utc(2026, 6, 12, 7), FRIDAY, 6) == _utc(2026, 6, 19, 6)


def test_weekly_exactly_on_slot_rolls_a_full_week():
    # Restart landing exactly on the slot must not double-fire.
    assert next_weekly_run(_utc(2026, 6, 12, 6), FRIDAY, 6) == _utc(2026, 6, 19, 6)
