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
