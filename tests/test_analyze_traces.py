"""Tests for analyze trace capture (Phase 1 of the LLM-as-judge eval workflow).

The trace table is the dataset source for offline evals — every captured row
is a real input/output pair we can later score against rubric variants. The
contract this test file pins down:

  1. Capture is gated by ANALYZE_TRACE_CAPTURE — off by default, on opt-in.
     This matters because the table holds raw user content; flipping it on
     must be an explicit operator decision, not a side effect of a deploy.
  2. When enabled, `analyze()` writes exactly one trace row alongside the
     existing `llm_calls` row, with provider/model/source_type and the
     structured output JSON.
  3. Writes are best-effort: a DB blip never breaks the analysis path.
  4. `retention_until` is stamped per row and `purge_expired_analyze_traces`
     deletes only rows whose TTL has elapsed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


_TOOL_INPUT = {
    "main_idea": "x", "why_it_matters": "y", "category": "c",
    "quick_win": "qw", "bigger_play": "bp", "time_required": "5m",
    "verdict": "watch",
}


class TestFlagGating:
    def test_capture_disabled_by_default(self, db, monkeypatch):
        monkeypatch.delenv("ANALYZE_TRACE_CAPTURE", raising=False)
        assert db.analyze_trace_capture_enabled() is False
        db.record_analyze_trace(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            source_type="article", input_text="hello", profile_text="",
            output={"main_idea": "x"},
        )
        assert _all_traces(db) == []

    def test_capture_enabled_with_truthy_values(self, db, monkeypatch):
        for val in ("1", "true", "TRUE", "yes"):
            monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", val)
            assert db.analyze_trace_capture_enabled() is True

    def test_capture_disabled_with_falsy_values(self, db, monkeypatch):
        for val in ("0", "false", "", "no"):
            monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", val)
            assert db.analyze_trace_capture_enabled() is False


class TestRecordHelper:
    def test_writes_row_with_all_columns(self, db, monkeypatch):
        monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", "1")
        db.record_analyze_trace(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            source_type="article",
            input_text="the source text",
            profile_text="about the user",
            output={"main_idea": "x", "verdict": "watch"},
            user_id=7, anon_id="anon-1", job_id="job-9",
        )
        rows = _all_traces(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["provider"] == "anthropic"
        assert r["model"] == "claude-haiku-4-5-20251001"
        assert r["source_type"] == "article"
        assert r["input_text"] == "the source text"
        assert r["profile_text"] == "about the user"
        assert json.loads(r["output_json"]) == {"main_idea": "x", "verdict": "watch"}
        assert r["user_id"] == 7
        assert r["anon_id"] == "anon-1"
        assert r["job_id"] == "job-9"
        assert r["retention_until"] > r["ts"]

    def test_anon_row_has_null_user_id(self, db, monkeypatch):
        monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", "1")
        db.record_analyze_trace(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            source_type="article", input_text="t", profile_text="",
            output={"main_idea": "x"}, anon_id="anon-xyz",
        )
        row = _all_traces(db)[0]
        assert row["user_id"] is None
        assert row["anon_id"] == "anon-xyz"

    def test_failure_to_write_does_not_raise(self, db, monkeypatch):
        # If the DB blows up mid-write the analysis must still succeed —
        # losing a trace row is acceptable, losing the user's analysis is not.
        monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", "1")

        def boom(*_a, **_kw):
            raise RuntimeError("disk gone")
        monkeypatch.setattr(db, "_get_conn", boom)

        db.record_analyze_trace(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            source_type="article", input_text="t", profile_text="",
            output={"main_idea": "x"},
        )  # should NOT raise

    def test_retention_respects_env(self, db, monkeypatch):
        # Use a 1-hour TTL and assert retention_until lands ~1h in the future.
        monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", "1")
        monkeypatch.setattr(db, "ANALYZE_TRACE_RETENTION_SECONDS", 3_600)
        db.record_analyze_trace(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            source_type="article", input_text="t", profile_text="",
            output={"main_idea": "x"},
        )
        row = _all_traces(db)[0]
        ts = datetime.fromisoformat(row["ts"]).replace(tzinfo=timezone.utc)
        until = datetime.fromisoformat(row["retention_until"]).replace(tzinfo=timezone.utc)
        delta = until - ts
        assert timedelta(minutes=59) <= delta <= timedelta(minutes=61)


class TestPurge:
    def test_purge_drops_only_expired_rows(self, db, monkeypatch):
        # Insert one row with retention in the past, one in the future. Only
        # the expired one should be deleted; the live one stays untouched.
        past = "2000-01-01T00:00:00"
        future = "2999-01-01T00:00:00"
        with db._get_conn() as conn:
            conn.execute(
                "INSERT INTO analyze_traces (provider, model, source_type, "
                "input_text, profile_text, output_json, retention_until) "
                "VALUES ('p','m','article','old','','{}',?)",
                (past,),
            )
            conn.execute(
                "INSERT INTO analyze_traces (provider, model, source_type, "
                "input_text, profile_text, output_json, retention_until) "
                "VALUES ('p','m','article','new','','{}',?)",
                (future,),
            )
        deleted = db.purge_expired_analyze_traces()
        assert deleted == 1
        rows = _all_traces(db)
        assert len(rows) == 1
        assert rows[0]["input_text"] == "new"


class TestAnalyzerHook:
    def test_analyze_writes_trace_when_enabled(self, db, monkeypatch):
        # End-to-end: with the flag on, one analyze() call produces one
        # llm_calls row (existing contract) AND one analyze_traces row that
        # carries the same input, the normalized output, and the source_type.
        monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", "1")
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        monkeypatch.setattr(analyzer, "_PREMIUM_MODEL", "claude-haiku-4-5-20251001")
        fake = _FakeAnthropic(input_tokens=10, output_tokens=5, tool_input=_TOOL_INPUT)
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        analyzer.analyze("the source text", ctx=analyzer.UsageContext(
            user_id=42, source_type="article", job_id="job-1",
        ))

        traces = _all_traces(db)
        assert len(traces) == 1
        t = traces[0]
        assert t["input_text"] == "the source text"
        assert t["source_type"] == "article"
        assert t["user_id"] == 42
        assert t["job_id"] == "job-1"
        out = json.loads(t["output_json"])
        assert out["main_idea"] == "x"
        assert out["verdict"] == "watch"

    def test_analyze_skips_trace_when_disabled(self, db, monkeypatch):
        monkeypatch.delenv("ANALYZE_TRACE_CAPTURE", raising=False)
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        monkeypatch.setattr(analyzer, "_PREMIUM_MODEL", "claude-haiku-4-5-20251001")
        fake = _FakeAnthropic(input_tokens=10, output_tokens=5, tool_input=_TOOL_INPUT)
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        analyzer.analyze("text", ctx=analyzer.UsageContext(user_id=1))

        assert _all_traces(db) == []
        # llm_calls must still be written — gating only affects traces.
        with db._get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1

    def test_analyze_failure_writes_no_trace(self, db, monkeypatch):
        # A failed call has no normalized output to capture; we still log the
        # error row in llm_calls (existing behaviour) but no trace.
        monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", "1")
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        monkeypatch.setattr(analyzer, "_PREMIUM_MODEL", "claude-haiku-4-5-20251001")

        class Boom:
            messages = SimpleNamespace(
                create=lambda **kw: (_ for _ in ()).throw(RuntimeError("rate-limited"))
            )
        monkeypatch.setattr(analyzer, "_get_client", lambda: Boom())

        try:
            analyzer.analyze("text", ctx=analyzer.UsageContext(user_id=1))
        except RuntimeError:
            pass

        assert _all_traces(db) == []

    def test_openai_path_also_captures(self, db, monkeypatch):
        monkeypatch.setenv("ANALYZE_TRACE_CAPTURE", "1")
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "openai")
        monkeypatch.setattr(analyzer, "_MODEL", "gpt-4o-mini")

        monkeypatch.setattr(analyzer, "_PREMIUM_MODEL", "gpt-4o-mini")

        fake = _FakeOpenAI(
            prompt_tokens=20, completion_tokens=10,
            content=json.dumps(_TOOL_INPUT),
        )
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        analyzer.analyze("text", ctx=analyzer.UsageContext(
            anon_id="anon-x", source_type="article",
        ))
        traces = _all_traces(db)
        assert len(traces) == 1
        assert traces[0]["provider"] == "openai"
        assert traces[0]["model"] == "gpt-4o-mini"
        assert traces[0]["anon_id"] == "anon-x"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_traces(db):
    with db._get_conn() as conn:
        return conn.execute("SELECT * FROM analyze_traces ORDER BY id").fetchall()


class _FakeAnthropic:
    def __init__(self, *, input_tokens, output_tokens, tool_input):
        content = [SimpleNamespace(type="tool_use", name="record_analysis", input=tool_input)]
        resp = SimpleNamespace(
            content=content,
            usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        )
        self.messages = SimpleNamespace(create=lambda **kw: resp)


class _FakeOpenAI:
    def __init__(self, *, prompt_tokens, completion_tokens, content):
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            ),
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp))
