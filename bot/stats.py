"""Per-user value stats (#53): what filter.fyi actually did for this person.

Powers the ROI strip on /me ("June: 14 reads filtered · 6 skips (~3.5 hrs not
read) · 3 actions done") and the digest header. Pure aggregation over stored
analyses and the Shortlist — no LLM calls.

The "time you didn't spend" figure is an estimate by construction: it sums
the analyzer's own `time_required` strings ("12 min read", "~1 hr") over
skip-verdict items at full value and skim-verdict items at half (a skim still
costs *some* time). The UI labels it as an estimate.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from bot.analyzer import parse_stored

# A skim isn't a full read avoided — count half.
SKIM_FACTOR = 0.5
MONTH_DAYS = 30

_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h\b|hrs?\b|hours?\b)", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+)\s*(?:m\b|mins?\b|minutes?\b)", re.IGNORECASE)


def parse_minutes(time_required: str) -> int:
    """Best-effort minutes from an analyzer time_required string.

    Handles "12 min read", "8 min watch", "~1 hr", "2 hrs", "1 h 30 min".
    Unparseable strings count as 0 — the estimate stays honest by
    under-counting, never inventing.
    """
    text = time_required or ""
    minutes = 0.0
    h = _HOURS_RE.search(text)
    if h:
        minutes += float(h.group(1)) * 60
    m = _MINUTES_RE.search(text)
    if m:
        minutes += int(m.group(1))
    return int(minutes)


def _window_stats(items, suggestions, since_iso: str | None) -> dict:
    out = {
        "items": 0, "watch": 0, "skim": 0, "skip": 0,
        "minutes_saved": 0,
        "suggestions_saved": 0, "suggestions_tried": 0, "suggestions_done": 0,
    }
    saved_minutes = 0.0
    for row in items:
        if since_iso and row["created_at"] < since_iso:
            continue
        out["items"] += 1
        analysis = parse_stored(row["analysis"]) or {}
        verdict = (analysis.get("verdict") or "").lower()
        if verdict in ("watch", "skim", "skip"):
            out[verdict] += 1
        minutes = parse_minutes(analysis.get("time_required") or "")
        if verdict == "skip":
            saved_minutes += minutes
        elif verdict == "skim":
            saved_minutes += minutes * SKIM_FACTOR
    out["minutes_saved"] = int(saved_minutes)

    for s in suggestions:
        # Saves are dated by creation; outcomes by their last status change.
        if s["status"] == "saved":
            if not since_iso or s["created_at"] >= since_iso:
                out["suggestions_saved"] += 1
        elif s["status"] in ("tried", "done"):
            if not since_iso or s["updated_at"] >= since_iso:
                out[f"suggestions_{s['status']}"] += 1
    return out


def build_user_stats(user_id: int, *, now: datetime | None = None) -> dict:
    """{"month": …, "all_time": …} for one user. Month = trailing 30 days."""
    from bot.db import get_all_items, get_saved_suggestions

    now = now or datetime.now(timezone.utc)
    since_iso = (now - timedelta(days=MONTH_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    items = get_all_items(user_id)
    suggestions = get_saved_suggestions(user_id)
    return {
        "month": _window_stats(items, suggestions, since_iso),
        "all_time": _window_stats(items, suggestions, None),
    }
