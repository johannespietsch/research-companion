"""Helpers for the in-process daily maintenance loops (error scan, prune).

These jobs run *inside* the bot process rather than as separate Fly scheduled
machines because the Fly volume holding the SQLite DB attaches to only one
machine at a time — a second machine couldn't open the same database. See the
loops in main.py.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone


def next_daily_run(now: datetime, hour_utc: int) -> datetime:
    """Return the next UTC datetime at ``hour_utc``:00 strictly after ``now``.

    If today's slot is still ahead it's used; if it has already passed (or is
    exactly ``now``) the slot rolls to tomorrow. Shared by the scan and prune
    loops so the scheduling logic lives (and is tested) in one place.
    """
    candidate = datetime.combine(now.date(), dtime(hour_utc, 0), tzinfo=timezone.utc)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
