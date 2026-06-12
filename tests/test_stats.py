"""Per-user value stats (#53): time parsing, window math, and the endpoint.

The honesty contract: minutes_saved only counts what the analyzer actually
estimated (unparseable strings count 0), skips at full value, skims at half.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


class TestParseMinutes:
    @pytest.mark.parametrize("text,minutes", [
        ("12 min read", 12),
        ("8 min watch", 8),
        ("~1 hr", 60),
        ("2 hrs", 120),
        ("1 h 30 min", 90),
        ("1.5 hours", 90),
        ("a quick look", 0),
        ("", 0),
    ])
    def test_variants(self, text, minutes):
        from bot.stats import parse_minutes
        assert parse_minutes(text) == minutes


def _add(db, uid, *, verdict, time_required="10 min read", days_ago=0):
    analysis = {"main_idea": "x", "why_it_matters": "", "grounded_in": "",
                "category": "ai", "time_required": time_required,
                "verdict": verdict, "suggestions": []}
    item_id = db.save_item(uid, "article", "https://ex.com/a", "c", json.dumps(analysis))
    if days_ago:
        ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        with db._get_conn() as conn:
            conn.execute("UPDATE items SET created_at = ? WHERE id = ?", (ts, item_id))
    return item_id


class TestBuildUserStats:
    def test_verdict_split_and_minutes_saved(self, db):
        from bot.stats import build_user_stats
        uid = db.upsert_user_by_email("u@example.com")
        _add(db, uid, verdict="watch", time_required="30 min watch")
        _add(db, uid, verdict="skip", time_required="20 min read")   # +20
        _add(db, uid, verdict="skim", time_required="10 min read")   # +5 (half)
        s = build_user_stats(uid, now=NOW)["month"]
        assert (s["items"], s["watch"], s["skim"], s["skip"]) == (3, 1, 1, 1)
        assert s["minutes_saved"] == 25  # watch items never count

    def test_month_window_vs_all_time(self, db):
        from bot.stats import build_user_stats
        uid = db.upsert_user_by_email("u@example.com")
        _add(db, uid, verdict="skip", time_required="60 min read", days_ago=40)
        _add(db, uid, verdict="skip", time_required="20 min read", days_ago=1)
        s = build_user_stats(uid, now=NOW)
        assert s["month"]["items"] == 1 and s["month"]["minutes_saved"] == 20
        assert s["all_time"]["items"] == 2 and s["all_time"]["minutes_saved"] == 80

    def test_suggestion_outcomes_counted_by_status(self, db):
        from bot.stats import build_user_stats
        uid = db.upsert_user_by_email("u@example.com")
        item = _add(db, uid, verdict="watch")
        for i, status in enumerate(["saved", "tried", "done"]):
            sid = db.save_suggestion(user_id=uid, item_id=item, suggestion_index=i,
                                     title=f"s{i}", detail="d", effort="",
                                     first_step="", grounded_in="")
            if status != "saved":
                db.update_saved_suggestion_status(sid, uid, status)
        s = build_user_stats(uid, now=NOW)["month"]
        assert (s["suggestions_saved"], s["suggestions_tried"], s["suggestions_done"]) == (1, 1, 1)

    def test_empty_user_is_all_zeroes(self, db):
        from bot.stats import build_user_stats
        uid = db.upsert_user_by_email("u@example.com")
        s = build_user_stats(uid, now=NOW)
        assert s["month"]["items"] == 0 and s["all_time"]["minutes_saved"] == 0


class TestStatsEndpoint:
    def test_returns_both_windows(self, client, auth_headers, db):
        uid = db.upsert_user_by_email("u@example.com")
        _add(db, uid, verdict="skip", time_required="15 min read")
        r = client.get(f"/api/users/{uid}/stats", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["month"]["minutes_saved"] == 15
        assert set(body) == {"month", "all_time"}

    def test_unknown_user_404(self, client, auth_headers):
        assert client.get("/api/users/99999/stats", headers=auth_headers).status_code == 404

    def test_requires_secret(self, client, db):
        uid = db.upsert_user_by_email("u@example.com")
        assert client.get(f"/api/users/{uid}/stats").status_code == 401
