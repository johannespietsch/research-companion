"""Behaviour-signal feedback loop (#69): storage, the day-coarsened digest,
its injection into analyze(), and the Worker-facing ingest endpoint.

The two contracts that matter most:
  1. The signal digest only changes once per UTC day (today's events are
     excluded), so the analyze cache key stays stable between meaningful
     changes instead of churning per click.
  2. Anonymous analyses are byte-identical to before — signals are signed-in
     only (data-isolation invariant).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

NOW = datetime(2026, 6, 12, 15, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _backdate_signal(db, signal_id: int, dt: datetime) -> None:
    with db._get_conn() as conn:
        conn.execute("UPDATE suggestion_signals SET created_at = ? WHERE id = ?",
                     (_iso(dt), signal_id))


def _dismiss(db, uid, text="Build a RAG demo", reason="too generic", *, days_ago=1):
    sid = db.record_suggestion_signal(uid, "dismiss", suggestion_text=text, reason=reason)
    _backdate_signal(db, sid, NOW - timedelta(days=days_ago))
    return sid


def _add_outcome(db, uid, *, status="done", title="Add a reranker", effort="~2 hrs",
                 days_ago=2):
    item_id = db.save_item(uid, "article", "https://ex.com/a", "c", "{}")
    saved_id = db.save_suggestion(
        user_id=uid, item_id=item_id, suggestion_index=0,
        title=title, detail="d", effort=effort, first_step="", grounded_in="",
    )
    if status != "saved":
        db.update_saved_suggestion_status(saved_id, uid, status)
    ts = _iso(NOW - timedelta(days=days_ago))
    with db._get_conn() as conn:
        conn.execute("UPDATE saved_suggestions SET created_at = ?, updated_at = ? WHERE id = ?",
                     (ts, ts, saved_id))
    return saved_id


class TestStorage:
    def test_record_and_fetch(self, db):
        uid = db.upsert_user_by_email("u@example.com")
        db.record_suggestion_signal(uid, "dismiss", url="https://ex.com",
                                    suggestion_index=2, suggestion_text="t", reason="r")
        rows = db.get_suggestion_signals(uid)
        assert len(rows) == 1
        assert rows[0]["event"] == "dismiss"
        assert rows[0]["suggestion_index"] == 2
        assert rows[0]["reason"] == "r"

    def test_values_are_clipped(self, db):
        uid = db.upsert_user_by_email("u@example.com")
        db.record_suggestion_signal(uid, "dismiss", reason="x" * 5000)
        assert len(db.get_suggestion_signals(uid)[0]["reason"]) == 2048

    def test_user_scoping(self, db):
        a = db.upsert_user_by_email("a@example.com")
        b = db.upsert_user_by_email("b@example.com")
        db.record_suggestion_signal(a, "dismiss", reason="mine")
        assert db.get_suggestion_signals(b) == []

    def test_delete_user_erases_signals(self, db):
        uid = db.upsert_user_by_email("u@example.com")
        db.record_suggestion_signal(uid, "dismiss")
        db.delete_user(uid)
        with db._get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM suggestion_signals").fetchone()[0]
        assert n == 0

    def test_prune_drops_old_signals_only(self, db):
        uid = db.upsert_user_by_email("u@example.com")
        old = db.record_suggestion_signal(uid, "dismiss", reason="ancient")
        _backdate_signal(db, old, NOW - timedelta(days=200))
        db.record_suggestion_signal(uid, "dismiss", reason="fresh")
        counts = db.prune_maintenance(now=NOW)
        assert counts["suggestion_signals"] == 1
        rows = db.get_suggestion_signals(uid)
        assert [r["reason"] for r in rows] == ["fresh"]


class TestSignalDigest:
    def test_anon_and_empty_users_get_empty_digest(self, db):
        from bot.signals import build_signal_digest
        assert build_signal_digest(None, now=NOW) == ""
        uid = db.upsert_user_by_email("u@example.com")
        assert build_signal_digest(uid, now=NOW) == ""

    def test_dismissals_with_reasons_are_included(self, db):
        from bot.signals import build_signal_digest
        uid = db.upsert_user_by_email("u@example.com")
        _dismiss(db, uid, text="Build a RAG demo", reason="too generic")
        d = build_signal_digest(uid, now=NOW)
        assert 'Dismissed "Build a RAG demo"' in d
        assert 'their reason: "too generic"' in d
        assert "never mention these signals" in d

    def test_todays_events_are_excluded_for_cache_stability(self, db):
        from bot.signals import build_signal_digest
        uid = db.upsert_user_by_email("u@example.com")
        sid = db.record_suggestion_signal(uid, "dismiss", suggestion_text="fresh", reason="r")
        _backdate_signal(db, sid, NOW - timedelta(hours=2))  # today, before NOW
        assert build_signal_digest(uid, now=NOW) == ""
        # The same event counts from tomorrow on.
        assert "fresh" in build_signal_digest(uid, now=NOW + timedelta(days=1))

    def test_stale_dismissals_age_out(self, db):
        from bot.signals import DISMISS_WINDOW_DAYS, build_signal_digest
        uid = db.upsert_user_by_email("u@example.com")
        _dismiss(db, uid, text="ancient", days_ago=DISMISS_WINDOW_DAYS + 5)
        assert build_signal_digest(uid, now=NOW) == ""

    def test_outcomes_and_stale_backlog_are_summarised(self, db):
        from bot.signals import PARKED_STALE_DAYS, build_signal_digest
        uid = db.upsert_user_by_email("u@example.com")
        _add_outcome(db, uid, status="done", title="Add a reranker", effort="~2 hrs")
        _add_outcome(db, uid, status="tried", title="Eval harness", effort="")
        _add_outcome(db, uid, status="saved", title="Parked thing",
                     days_ago=PARKED_STALE_DAYS + 1)
        d = build_signal_digest(uid, now=NOW)
        assert 'Completed "Add a reranker" (~2 hrs)' in d
        assert 'Tried "Eval harness"' in d
        assert "1 earlier suggestion still parked" in d

    def test_digest_is_bounded(self, db):
        from bot.signals import SIGNAL_DIGEST_MAX_CHARS, build_signal_digest
        uid = db.upsert_user_by_email("u@example.com")
        for i in range(20):
            _dismiss(db, uid, text=f"suggestion {i} " + "x" * 200, reason="y" * 200)
        assert len(build_signal_digest(uid, now=NOW)) <= SIGNAL_DIGEST_MAX_CHARS


class _CapturingAnthropic:
    """Fake Anthropic client that records the prompt it was called with."""

    def __init__(self, tool_input):
        self.prompts: list[str] = []

        def create(**kw):
            self.prompts.append(kw["messages"][0]["content"])
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name="record_analysis",
                                         input=tool_input)],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

        self.messages = SimpleNamespace(create=create)


_TOOL_INPUT = {
    "main_idea": "x", "why_it_matters": "y", "grounded_in": "g", "category": "c",
    "time_required": "5m", "verdict": "watch", "suggestions": [],
}


@pytest.fixture
def fake_analyzer(db, monkeypatch):
    from bot import analyzer
    monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
    monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setattr(analyzer, "_PREMIUM_MODEL", "claude-haiku-4-5-20251001")
    fake = _CapturingAnthropic(_TOOL_INPUT)
    monkeypatch.setattr(analyzer, "_get_client", lambda: fake)
    return analyzer, fake


class TestAnalyzerIntegration:
    def test_signals_block_reaches_the_prompt(self, db, fake_analyzer, monkeypatch):
        analyzer, fake = fake_analyzer
        uid = db.upsert_user_by_email("u@example.com")
        _dismiss(db, uid, reason="too generic")
        monkeypatch.setattr(analyzer, "_load_signals",
                            lambda user_id: __import__("bot.signals", fromlist=["build_signal_digest"]).build_signal_digest(user_id, now=NOW))
        analyzer.analyze("content", ctx=analyzer.UsageContext(user_id=uid))
        assert "too generic" in fake.prompts[0]

    def test_anon_prompt_carries_no_signals_block(self, db, fake_analyzer):
        analyzer, fake = fake_analyzer
        analyzer.analyze("content", ctx=analyzer.UsageContext(user_id=None))
        assert "behaviour signals" not in fake.prompts[0].lower()

    def test_cache_key_changes_with_signals(self, db):
        from bot import analyzer
        base = analyzer._cache_key_analyze("t", "p", "m")
        assert analyzer._cache_key_analyze("t", "p", "m", "sig") != base
        assert analyzer._cache_key_analyze("t", "p", "m", "") == base

    def test_signal_failure_never_breaks_analysis(self, db, fake_analyzer, monkeypatch):
        analyzer, fake = fake_analyzer
        uid = db.upsert_user_by_email("u@example.com")

        def boom(user_id, **kw):
            raise RuntimeError("db hiccup")

        import bot.signals
        monkeypatch.setattr(bot.signals, "build_signal_digest", boom)
        result = analyzer.analyze("content", ctx=analyzer.UsageContext(user_id=uid))
        assert result["verdict"] == "watch"


class TestIngestEndpoint:
    def test_records_a_signal(self, client, auth_headers, db):
        uid = db.upsert_user_by_email("u@example.com")
        r = client.post("/api/suggestion-signals", headers=auth_headers, json={
            "user_id": uid, "event": "dismiss", "url": "https://ex.com",
            "suggestion_index": 1, "suggestion_text": "t", "reason": "too generic",
        })
        assert r.status_code == 201
        rows = db.get_suggestion_signals(uid)
        assert rows[0]["reason"] == "too generic"

    def test_invalid_event_rejected(self, client, auth_headers, db):
        uid = db.upsert_user_by_email("u@example.com")
        r = client.post("/api/suggestion-signals", headers=auth_headers,
                        json={"user_id": uid, "event": "totally-made-up"})
        assert r.status_code == 400

    def test_unknown_user_rejected(self, client, auth_headers):
        r = client.post("/api/suggestion-signals", headers=auth_headers,
                        json={"user_id": 99999, "event": "dismiss"})
        assert r.status_code == 404

    def test_requires_shared_secret(self, client):
        r = client.post("/api/suggestion-signals",
                        json={"user_id": 1, "event": "dismiss"})
        assert r.status_code == 401

    def test_export_includes_signals(self, client, auth_headers, db):
        uid = db.upsert_user_by_email("u@example.com")
        db.record_suggestion_signal(uid, "dismiss", reason="too generic")
        r = client.get(f"/api/users/{uid}/export", headers=auth_headers)
        assert r.status_code == 200
        sig = r.json()["suggestion_signals"]
        assert len(sig) == 1 and sig[0]["reason"] == "too generic"
