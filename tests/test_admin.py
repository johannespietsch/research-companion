"""Tests for the /api/admin/* endpoints.

Three things matter here:
  1. Auth — the admin secret is separate from the try-secret. Wrong/missing
     header rejects; misconfigured server (no secret) fails closed.
  2. Aggregations — the SQL queries against `llm_calls` produce the shape and
     numbers the Worker's renderer expects (totals, daily series with
     zero-filled days, source/purpose breakdowns, identity split, top users).
  3. Date filtering — only rows inside the window count.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_client(monkeypatch):
    """A TestClient mounted on the admin router with the secret pre-set."""
    monkeypatch.setenv("FILTER_FYI_ADMIN_SECRET", "test-admin-secret")
    import bot.admin
    app = FastAPI()
    app.include_router(bot.admin.router)
    return TestClient(app)


@pytest.fixture
def admin_headers():
    return {"x-filter-fyi-admin-secret": "test-admin-secret"}


def _stamp(db, *, days_ago: int = 0, **overrides) -> None:
    """Insert one llm_calls row with `ts` set N days in the past.

    `insert_llm_call` uses sqlite's `strftime('now')` default for `ts`, so it
    can't be used directly for backdated rows — write through a raw cursor
    instead. Defaults match a successful Anthropic analyze call.
    """
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    row = {
        "user_id": None,
        "anon_id": None,
        "job_id": None,
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "purpose": "analyze",
        "source_type": "article",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.00035,
        "latency_ms": 500,
        "status": "ok",
        "error": "",
    }
    row.update(overrides)
    with db._get_conn() as conn:
        conn.execute(
            "INSERT INTO llm_calls (ts, user_id, anon_id, job_id, provider, "
            "model, purpose, source_type, input_tokens, output_tokens, "
            "cost_usd, latency_ms, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, row["user_id"], row["anon_id"], row["job_id"], row["provider"],
                row["model"], row["purpose"], row["source_type"],
                row["input_tokens"], row["output_tokens"], row["cost_usd"],
                row["latency_ms"], row["status"], row["error"],
            ),
        )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAdminAuth:
    def test_correct_secret_accepted(self, admin_client, admin_headers):
        r = admin_client.get("/api/admin/cost-overview", headers=admin_headers)
        assert r.status_code == 200

    def test_missing_header_rejected(self, admin_client):
        r = admin_client.get("/api/admin/cost-overview")
        assert r.status_code == 401

    def test_wrong_secret_rejected(self, admin_client):
        r = admin_client.get(
            "/api/admin/cost-overview",
            headers={"x-filter-fyi-admin-secret": "nope"},
        )
        assert r.status_code == 401

    def test_no_configured_secret_fails_closed(self, admin_client, monkeypatch):
        # An admin endpoint with no secret configured must never serve, even
        # when the caller provides a (random) header.
        monkeypatch.delenv("FILTER_FYI_ADMIN_SECRET", raising=False)
        r = admin_client.get(
            "/api/admin/cost-overview",
            headers={"x-filter-fyi-admin-secret": "anything"},
        )
        assert r.status_code == 503

    def test_admin_secret_independent_of_try_secret(self, admin_client, monkeypatch):
        # Setting the try-secret alone must not grant admin access. The point
        # of separating the two is that a try-secret leak doesn't give admin.
        monkeypatch.setenv("FILTER_FYI_TRY_SECRET", "test-secret")
        r = admin_client.get(
            "/api/admin/cost-overview",
            headers={"x-filter-fyi-secret": "test-secret"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Cost overview shape & aggregations
# ---------------------------------------------------------------------------

class TestCostOverviewEmpty:
    def test_empty_table_returns_zero_kpis_and_daily_backfill(
        self, admin_client, admin_headers
    ):
        r = admin_client.get(
            "/api/admin/cost-overview?days=7", headers=admin_headers
        )
        assert r.status_code == 200
        body = r.json()
        assert body["range_days"] == 7
        assert body["kpis"] == {
            "total_calls": 0,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "errors": 0,
            "error_rate": 0.0,
        }
        # Daily series still has 7 entries (the backfill is what the
        # renderer relies on for a contiguous sparkline).
        assert len(body["daily"]) == 7
        assert all(d["calls"] == 0 and d["cost_usd"] == 0.0 for d in body["daily"])
        assert body["by_source_type"] == []
        assert body["by_purpose"] == []
        assert body["top_users"] == []
        # by_identity is always two rows even when empty.
        kinds = {row["kind"] for row in body["by_identity"]}
        assert kinds == {"signed_in", "anon"}


class TestCostOverviewWithData:
    def test_kpis_sum_across_all_rows(self, db, admin_client, admin_headers):
        _stamp(db, input_tokens=100, output_tokens=50, cost_usd=0.0005)
        _stamp(db, input_tokens=200, output_tokens=80, cost_usd=0.001)
        _stamp(db, status="error", input_tokens=0, output_tokens=0, cost_usd=0)

        r = admin_client.get("/api/admin/cost-overview", headers=admin_headers)
        body = r.json()
        kpis = body["kpis"]
        assert kpis["total_calls"] == 3
        assert kpis["total_input_tokens"] == 300
        assert kpis["total_output_tokens"] == 130
        assert abs(kpis["total_cost_usd"] - 0.0015) < 1e-9
        assert kpis["errors"] == 1
        assert kpis["error_rate"] == round(1 / 3, 4)

    def test_daily_groups_by_date_and_backfills_zero_days(
        self, db, admin_client, admin_headers
    ):
        _stamp(db, days_ago=0, cost_usd=0.001)
        _stamp(db, days_ago=0, cost_usd=0.002)  # same day → 0.003
        _stamp(db, days_ago=2, cost_usd=0.004)  # gap day at days_ago=1

        r = admin_client.get(
            "/api/admin/cost-overview?days=3", headers=admin_headers
        )
        body = r.json()
        daily = body["daily"]
        # Series is ordered oldest-first across the full window.
        assert len(daily) == 3
        assert daily[0]["cost_usd"] - 0.004 < 1e-9  # 2 days ago
        assert daily[1]["cost_usd"] == 0.0          # 1 day ago — backfilled
        assert daily[1]["calls"] == 0
        assert abs(daily[2]["cost_usd"] - 0.003) < 1e-9  # today

    def test_by_source_type_sorts_by_cost_and_excludes_errors(
        self, db, admin_client, admin_headers
    ):
        _stamp(db, source_type="article", cost_usd=0.001)
        _stamp(db, source_type="article", cost_usd=0.002)
        _stamp(db, source_type="photo", cost_usd=0.005)
        _stamp(db, source_type="photo", status="error", cost_usd=0)  # excluded

        r = admin_client.get("/api/admin/cost-overview", headers=admin_headers)
        rows = r.json()["by_source_type"]
        assert [row["source_type"] for row in rows] == ["photo", "article"]
        assert rows[0]["calls"] == 1   # error excluded
        assert rows[1]["calls"] == 2

    def test_by_purpose_includes_errors_in_count_but_not_avg_latency(
        self, db, admin_client, admin_headers
    ):
        _stamp(db, purpose="analyze", latency_ms=400)
        _stamp(db, purpose="analyze", latency_ms=600)
        # Error rows have latency_ms but zero tokens — excluding them from the
        # avg keeps "how long does a real call take" honest.
        _stamp(db, purpose="analyze", status="error", latency_ms=10, cost_usd=0)
        _stamp(db, purpose="summary", latency_ms=1200, cost_usd=0.002)

        r = admin_client.get("/api/admin/cost-overview", headers=admin_headers)
        by_purpose = {row["purpose"]: row for row in r.json()["by_purpose"]}
        assert by_purpose["analyze"]["calls"] == 3
        assert by_purpose["analyze"]["errors"] == 1
        assert by_purpose["analyze"]["avg_latency_ms"] == 500  # (400+600)/2
        assert by_purpose["summary"]["calls"] == 1
        assert by_purpose["summary"]["errors"] == 0

    def test_by_identity_splits_anon_and_signed_in(
        self, db, admin_client, admin_headers
    ):
        _stamp(db, user_id=1, cost_usd=0.001)
        _stamp(db, user_id=1, cost_usd=0.001)  # same user → 1 unique actor
        _stamp(db, user_id=2, cost_usd=0.001)  # 2 distinct users total
        _stamp(db, anon_id="anon-A", cost_usd=0.002)
        _stamp(db, anon_id="anon-A", cost_usd=0.002)  # same anon → 1 unique
        _stamp(db, anon_id="anon-B", cost_usd=0.002)  # 2 distinct anons total

        r = admin_client.get("/api/admin/cost-overview", headers=admin_headers)
        by_kind = {row["kind"]: row for row in r.json()["by_identity"]}
        assert by_kind["signed_in"]["calls"] == 3
        assert by_kind["signed_in"]["unique_actors"] == 2
        assert by_kind["anon"]["calls"] == 3
        assert by_kind["anon"]["unique_actors"] == 2

    def test_top_users_capped_and_excludes_anon(
        self, db, admin_client, admin_headers
    ):
        # 12 users, decreasing cost. Top 10 returned, anon row not in the list.
        for i in range(12):
            _stamp(db, user_id=i + 1, cost_usd=(12 - i) * 0.001)
        _stamp(db, anon_id="anon-xyz", cost_usd=999)

        r = admin_client.get("/api/admin/cost-overview", headers=admin_headers)
        top = r.json()["top_users"]
        assert len(top) == 10
        assert top[0]["user_id"] == 1   # highest cost (12 * 0.001)
        assert top[0]["cost_usd"] - 0.012 < 1e-9
        assert all(t["user_id"] != "anon-xyz" for t in top)


class TestCacheStats:
    """The /api/admin/cost-overview response carries a `cache` block with hit
    counts and estimated savings — the data behind the dashboard's cache tile."""

    def test_empty_cache_block_when_no_hits(self, admin_client, admin_headers):
        body = admin_client.get(
            "/api/admin/cost-overview", headers=admin_headers
        ).json()
        assert body["cache"] == {
            "hits": 0,
            "cost_saved_usd": 0.0,
            "hit_rate": 0.0,
            "by_purpose": [],
        }

    def test_hits_aggregated_with_estimated_savings(
        self, db, admin_client, admin_headers
    ):
        # Two analyze hits (saving $0.002 each) + one summary hit ($0.01).
        db.record_cache_hit(purpose="analyze", cost_saved_usd=0.002)
        db.record_cache_hit(purpose="analyze", cost_saved_usd=0.002)
        db.record_cache_hit(purpose="summary", cost_saved_usd=0.010)
        # Plus one real upstream analyze (so hit_rate has a denominator).
        _stamp(db, purpose="analyze")

        body = admin_client.get(
            "/api/admin/cost-overview", headers=admin_headers
        ).json()
        cache = body["cache"]
        assert cache["hits"] == 3
        assert abs(cache["cost_saved_usd"] - 0.014) < 1e-9
        # hit_rate = 3 hits / (3 hits + 1 cacheable real call) = 0.75
        assert cache["hit_rate"] == 0.75
        by = {row["purpose"]: row for row in cache["by_purpose"]}
        assert by["analyze"]["hits"] == 2
        assert by["summary"]["hits"] == 1
        assert abs(by["analyze"]["cost_saved_usd"] - 0.004) < 1e-9


def _stamp_url(db, *, days_ago: int = 0, **overrides) -> None:
    """Insert one processed_urls row with `ts` set N days ago. Defaults to a
    successful article fetch — tests pass `status='error'`, `error_code=`,
    `transcript_source=`, etc. when they need to exercise specific cases."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    row = {
        "url": "https://example.com/post",
        "title": "A title",
        "source_type": "article",
        "user_id": None,
        "anon_id": None,
        "job_id": None,
        "status": "ok",
        "error_code": "",
        "transcript_source": "",
        "latency_ms": 500,
    }
    row.update(overrides)
    with db._get_conn() as conn:
        conn.execute(
            "INSERT INTO processed_urls (ts, url, title, source_type, "
            "user_id, anon_id, job_id, status, error_code, transcript_source, "
            "latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, row["url"], row["title"], row["source_type"],
                row["user_id"], row["anon_id"], row["job_id"], row["status"],
                row["error_code"], row["transcript_source"], row["latency_ms"],
            ),
        )


class TestUsageOverviewEmpty:
    def test_empty_table_returns_zero_kpis_and_empty_lists(
        self, admin_client, admin_headers
    ):
        body = admin_client.get(
            "/api/admin/usage-overview?days=7", headers=admin_headers
        ).json()
        assert body["range_days"] == 7
        assert body["kpis"] == {
            "total": 0, "ok": 0, "errors": 0, "error_rate": 0.0,
        }
        assert body["by_source_type"] == []
        assert body["by_error_code"] == []
        assert body["transcript_sources"] == []
        assert body["rows"] == []
        assert body["total_rows"] == 0


class TestUsageOverviewWithData:
    def test_kpis_count_ok_and_errors(self, db, admin_client, admin_headers):
        _stamp_url(db, status="ok")
        _stamp_url(db, status="ok")
        _stamp_url(db, status="error", error_code="fetch-failed")

        body = admin_client.get(
            "/api/admin/usage-overview", headers=admin_headers
        ).json()
        assert body["kpis"]["total"] == 3
        assert body["kpis"]["ok"] == 2
        assert body["kpis"]["errors"] == 1
        assert body["kpis"]["error_rate"] == round(1 / 3, 4)

    def test_by_source_type_breakdown(self, db, admin_client, admin_headers):
        _stamp_url(db, source_type="article", status="ok")
        _stamp_url(db, source_type="article", status="error", error_code="x")
        _stamp_url(db, source_type="youtube", status="ok")

        rows = admin_client.get(
            "/api/admin/usage-overview", headers=admin_headers
        ).json()["by_source_type"]
        by_kind = {r["source_type"]: r for r in rows}
        assert by_kind["article"]["total"] == 2
        assert by_kind["article"]["ok"] == 1
        assert by_kind["article"]["errors"] == 1
        assert by_kind["youtube"]["ok"] == 1

    def test_by_error_code_sorted_by_frequency(self, db, admin_client, admin_headers):
        for _ in range(3):
            _stamp_url(db, status="error", error_code="fetch-failed")
        for _ in range(2):
            _stamp_url(db, status="error", error_code="no-transcript")
        # Success rows don't appear in the error breakdown.
        _stamp_url(db, status="ok")

        rows = admin_client.get(
            "/api/admin/usage-overview", headers=admin_headers
        ).json()["by_error_code"]
        assert [r["error_code"] for r in rows] == ["fetch-failed", "no-transcript"]
        assert rows[0]["count"] == 3
        assert rows[1]["count"] == 2

    def test_transcript_sources_only_count_video_rows(
        self, db, admin_client, admin_headers
    ):
        # Articles have no meaningful transcript_source — must not appear.
        _stamp_url(db, source_type="article", transcript_source="")
        # 3 YouTube videos: 2 used YT captions, 1 used Whisper.
        _stamp_url(db, source_type="youtube", transcript_source="youtube")
        _stamp_url(db, source_type="youtube", transcript_source="youtube")
        _stamp_url(db, source_type="youtube", transcript_source="whisper")
        # Plus one description-only fallback.
        _stamp_url(db, source_type="youtube", transcript_source="description")

        rows = admin_client.get(
            "/api/admin/usage-overview", headers=admin_headers
        ).json()["transcript_sources"]
        by_source = {r["source"]: r["count"] for r in rows}
        assert by_source == {"youtube": 2, "whisper": 1, "description": 1}

    def test_rows_paginate_and_total_matches_window(
        self, db, admin_client, admin_headers
    ):
        for i in range(5):
            _stamp_url(db, url=f"https://example.com/p{i}")

        page1 = admin_client.get(
            "/api/admin/usage-overview?limit=2&offset=0", headers=admin_headers
        ).json()
        page2 = admin_client.get(
            "/api/admin/usage-overview?limit=2&offset=2", headers=admin_headers
        ).json()

        assert page1["total_rows"] == 5
        assert page2["total_rows"] == 5
        assert len(page1["rows"]) == 2
        assert len(page2["rows"]) == 2
        # Pages don't overlap.
        ids_page1 = {r["id"] for r in page1["rows"]}
        ids_page2 = {r["id"] for r in page2["rows"]}
        assert ids_page1.isdisjoint(ids_page2)

    def test_rows_ordered_newest_first(self, db, admin_client, admin_headers):
        _stamp_url(db, url="https://old", days_ago=2)
        _stamp_url(db, url="https://new", days_ago=0)

        rows = admin_client.get(
            "/api/admin/usage-overview", headers=admin_headers
        ).json()["rows"]
        assert rows[0]["url"] == "https://new"
        assert rows[1]["url"] == "https://old"


class TestUsageRangeFiltering:
    def test_rows_outside_window_excluded(
        self, db, admin_client, admin_headers
    ):
        _stamp_url(db, url="https://inside", days_ago=2)
        _stamp_url(db, url="https://outside", days_ago=10)

        body = admin_client.get(
            "/api/admin/usage-overview?days=7", headers=admin_headers
        ).json()
        assert body["kpis"]["total"] == 1
        assert [r["url"] for r in body["rows"]] == ["https://inside"]


class TestRangeFiltering:
    def test_rows_older_than_window_excluded(
        self, db, admin_client, admin_headers
    ):
        _stamp(db, days_ago=2, cost_usd=0.001)
        _stamp(db, days_ago=10, cost_usd=0.999)  # outside a 7-day window
        r = admin_client.get(
            "/api/admin/cost-overview?days=7", headers=admin_headers
        )
        body = r.json()
        assert body["kpis"]["total_calls"] == 1
        assert abs(body["kpis"]["total_cost_usd"] - 0.001) < 1e-9

    def test_days_param_validated(self, admin_client, admin_headers):
        # FastAPI's Query(ge=1, le=365) should reject out-of-range values.
        assert admin_client.get(
            "/api/admin/cost-overview?days=0", headers=admin_headers
        ).status_code == 422
        assert admin_client.get(
            "/api/admin/cost-overview?days=10000", headers=admin_headers
        ).status_code == 422


# ---------------------------------------------------------------------------
# Retrigger
# ---------------------------------------------------------------------------

@pytest.fixture
def retrigger_client(monkeypatch):
    """Same as `admin_client`, but with the pipeline's external dependencies
    (fetch/summarize/analyze) stubbed the way tests/conftest.py's `client`
    fixture does for bot.api — retrigger runs the same bot.pipeline chain."""
    monkeypatch.setenv("FILTER_FYI_ADMIN_SECRET", "test-admin-secret")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import bot.admin
    import bot.pipeline

    async def fake_fetch_url(url, **kwargs):
        return {
            "text": "Fresh full body, not just a title.",
            "title": "A Title",
            "source_type": "social",
            "image_urls": [],
        }

    def fake_analyze(text, user_id=None, **_kwargs):
        return {
            "main_idea": "x", "why_it_matters": "y", "category": "c",
            "suggestions": [], "time_required": "5m", "verdict": "watch",
        }

    def fake_summarize_content(text, **_kwargs):
        return "Neutral summary of the content."

    monkeypatch.setattr(bot.pipeline, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(bot.pipeline, "analyze", fake_analyze)
    monkeypatch.setattr(bot.pipeline, "summarize_content", fake_summarize_content)

    app = FastAPI()
    app.include_router(bot.admin.router)
    return TestClient(app)


class TestRetrigger:
    def test_missing_secret_rejected(self, retrigger_client):
        r = retrigger_client.post(
            "/api/admin/retrigger", json={"url": "https://x.com/a/status/1"}
        )
        assert r.status_code == 401

    def test_invalid_url_rejected(self, retrigger_client, admin_headers):
        r = retrigger_client.post(
            "/api/admin/retrigger", json={"url": "not-a-url"}, headers=admin_headers,
        )
        assert r.status_code == 400

    def test_success_returns_fresh_result(self, retrigger_client, admin_headers):
        r = retrigger_client.post(
            "/api/admin/retrigger",
            json={"url": "https://x.com/a/status/1"},
            headers=admin_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["title"] == "A Title"
        assert body["source_type"] == "social"
        assert body["verdict"] == "watch"

    def test_writes_a_fresh_processed_urls_row(self, db, retrigger_client, admin_headers):
        retrigger_client.post(
            "/api/admin/retrigger",
            json={"url": "https://x.com/a/status/1"},
            headers=admin_headers,
        )
        with db._get_conn() as conn:
            rows = conn.execute(
                "SELECT url, status, anon_id FROM processed_urls"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["url"] == "https://x.com/a/status/1"
        assert rows[0]["status"] == "ok"
        assert rows[0]["anon_id"] == "admin-retrigger"

    def test_bypasses_url_cache(self, db, retrigger_client, admin_headers):
        """A stale url_cache entry (the exact scenario #103's fix needs to
        repair post-deploy) must not shadow the retriggered fetch."""
        db.set_cached_fetch(
            "https://x.com/a/status/1",
            {"text": "STALE title-only stub", "title": "Stale", "source_type": "social"},
        )
        r = retrigger_client.post(
            "/api/admin/retrigger",
            json={"url": "https://x.com/a/status/1"},
            headers=admin_headers,
        )
        assert r.json()["title"] == "A Title", "must fetch fresh, not the stale cache entry"

    def test_pipeline_error_reported_not_raised(self, retrigger_client, admin_headers, monkeypatch):
        import bot.pipeline

        async def empty_fetch(url, **kwargs):
            return {"text": "", "title": "", "source_type": "article"}
        monkeypatch.setattr(bot.pipeline, "fetch_url", empty_fetch)

        r = retrigger_client.post(
            "/api/admin/retrigger",
            json={"url": "https://example.com/empty"},
            headers=admin_headers,
        )
        assert r.status_code == 200, "a still-broken URL is a valid retrigger outcome, not an HTTP error"
        body = r.json()
        assert body["status"] == "error"
        assert body["error_code"] == "extraction-failed"
