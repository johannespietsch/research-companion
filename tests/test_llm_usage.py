"""Tests for the LLM usage log.

Covers the three things we need to keep honest:
  1. Pricing math — known models compute, unknown models fall back to 0.
  2. Schema + helper — `insert_llm_call` writes the columns we expect, and
     a failure inside it never escapes into the analyzer.
  3. End-to-end capture — each public analyzer function writes exactly one row
     per upstream call, including failures (status='error', tokens=0).
"""
from __future__ import annotations

from types import SimpleNamespace


class TestPricing:
    def test_known_model_charges_input_and_output(self):
        from bot import pricing
        # claude-haiku-4-5: (1.0, 5.0) per MTok
        c = pricing.cost_usd("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        assert c == 6.0
        # 1k input + 2k output at $1/$5 per MTok = $0.001 + $0.010 = $0.011
        c = pricing.cost_usd("claude-haiku-4-5-20251001", 1_000, 2_000)
        assert round(c, 6) == 0.011

    def test_unknown_model_costs_zero(self):
        from bot import pricing
        assert pricing.cost_usd("not-a-real-model", 10_000, 10_000) == 0.0


class TestInsertHelper:
    def test_writes_row_with_all_columns(self, db):
        db.insert_llm_call(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            purpose="analyze", input_tokens=100, output_tokens=50,
            cost_usd=0.00035, latency_ms=420, status="ok", error="",
            user_id=7, anon_id="abc-123", job_id="job-9", source_type="article",
        )
        rows = _all_llm_calls(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["provider"] == "anthropic"
        assert r["model"] == "claude-haiku-4-5-20251001"
        assert r["purpose"] == "analyze"
        assert r["input_tokens"] == 100
        assert r["output_tokens"] == 50
        assert abs(r["cost_usd"] - 0.00035) < 1e-9
        assert r["latency_ms"] == 420
        assert r["status"] == "ok"
        assert r["user_id"] == 7
        assert r["anon_id"] == "abc-123"
        assert r["job_id"] == "job-9"
        assert r["source_type"] == "article"

    def test_anon_row_has_null_user_id(self, db):
        db.insert_llm_call(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            purpose="analyze", input_tokens=10, output_tokens=5,
            cost_usd=0.0, latency_ms=10, anon_id="anon-xyz",
        )
        row = _all_llm_calls(db)[0]
        assert row["user_id"] is None
        assert row["anon_id"] == "anon-xyz"

    def test_failure_to_write_does_not_raise(self, db, monkeypatch):
        # Simulate the DB being unavailable mid-call. The helper must swallow it
        # so a logging blip never breaks an analysis request.
        def boom(*_a, **_kw):
            raise RuntimeError("disk gone")
        monkeypatch.setattr(db, "_get_conn", boom)
        db.insert_llm_call(
            provider="anthropic", model="claude-haiku-4-5-20251001",
            purpose="analyze", input_tokens=0, output_tokens=0,
            cost_usd=0.0, latency_ms=0,
        )  # should NOT raise


class TestAnthropicCapture:
    def test_analyze_logs_one_row_with_tokens_and_cost(self, db, monkeypatch):
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        fake = _FakeAnthropic(input_tokens=200, output_tokens=80, tool_input={
            "main_idea": "x", "why_it_matters": "y", "category": "c",
            "quick_win": "qw", "bigger_play": "bp", "time_required": "5m",
            "verdict": "watch",
        })
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        out = analyzer.analyze("hello", ctx=analyzer.UsageContext(
            user_id=42, source_type="article",
        ))
        assert out["verdict"] == "watch"

        rows = _all_llm_calls(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["provider"] == "anthropic"
        assert r["purpose"] == "analyze"
        assert r["input_tokens"] == 200
        assert r["output_tokens"] == 80
        assert r["status"] == "ok"
        assert r["user_id"] == 42
        assert r["source_type"] == "article"
        # 200 in @ $1/MTok + 80 out @ $5/MTok = 0.0002 + 0.0004 = 0.0006
        assert abs(r["cost_usd"] - 0.0006) < 1e-9

    def test_summarize_content_logs_summary_purpose(self, db, monkeypatch):
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        fake = _FakeAnthropic(input_tokens=900, output_tokens=300, text="A summary.")
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        out = analyzer.summarize_content("a bit of text", ctx=analyzer.UsageContext(
            anon_id="anon-1", source_type="article",
        ))
        assert out == "A summary."

        rows = _all_llm_calls(db)
        assert len(rows) == 1 and rows[0]["purpose"] == "summary"
        assert rows[0]["anon_id"] == "anon-1"
        assert rows[0]["input_tokens"] == 900 and rows[0]["output_tokens"] == 300

    def test_failure_logs_error_row_and_reraises(self, db, monkeypatch):
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        class Boom:
            messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("rate-limited")))
        monkeypatch.setattr(analyzer, "_get_client", lambda: Boom())

        try:
            analyzer.analyze("x", ctx=analyzer.UsageContext(user_id=1))
            raise AssertionError("expected analyze to re-raise")
        except RuntimeError:
            pass

        rows = _all_llm_calls(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["status"] == "error"
        assert "rate-limited" in r["error"]
        assert r["input_tokens"] == 0 and r["output_tokens"] == 0
        assert r["cost_usd"] == 0.0


class TestResultCache:
    """analyze() and summarize_content() check llm_cache before calling the
    LLM. A second call with identical (model, prompt, content) must skip the
    upstream entirely — no llm_calls row written, no token spend, same
    output returned."""

    def test_analyze_second_call_is_a_cache_hit(self, db, monkeypatch):
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        fake = _FakeAnthropic(input_tokens=100, output_tokens=50, tool_input={
            "main_idea": "x", "why_it_matters": "y", "category": "c",
            "quick_win": "qw", "bigger_play": "bp", "time_required": "5m",
            "verdict": "watch",
        })
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        first = analyzer.analyze("the same text", ctx=analyzer.UsageContext(
            user_id=1, source_type="article"
        ))
        second = analyzer.analyze("the same text", ctx=analyzer.UsageContext(
            user_id=1, source_type="article"
        ))
        # Identical output, but only ONE llm_calls row — the second call
        # hit the cache and skipped the upstream.
        assert first == second
        rows = _all_llm_calls(db)
        assert len(rows) == 1, f"expected 1 LLM call after cache hit, got {len(rows)}"

    def test_analyze_invalidated_by_profile_change(self, db, monkeypatch):
        """Same text but different profile → different cache key → real call."""
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        # Two distinct users with distinct profiles. The user_id flows into
        # _load_profile, which reads users.profile — set them explicitly so
        # the cache key differs between them.
        u1 = db.get_or_create_user_by_telegram(1)
        u2 = db.get_or_create_user_by_telegram(2)
        db.set_user_profile(u1, "Profile A")
        db.set_user_profile(u2, "Profile B")

        fake = _FakeAnthropic(input_tokens=100, output_tokens=50, tool_input={
            "main_idea": "x", "why_it_matters": "y", "category": "c",
            "quick_win": "qw", "bigger_play": "bp", "time_required": "5m",
            "verdict": "watch",
        })
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        analyzer.analyze("same text", ctx=analyzer.UsageContext(user_id=u1))
        analyzer.analyze("same text", ctx=analyzer.UsageContext(user_id=u2))
        # Two different profiles → two different cache keys → two upstream
        # calls. (Cache only helps within a single profile-content pair.)
        assert len(_all_llm_calls(db)) == 2

    def test_summarize_content_second_call_is_a_cache_hit(self, db, monkeypatch):
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        fake = _FakeAnthropic(input_tokens=500, output_tokens=200, text="A summary.")
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        first = analyzer.summarize_content("the source text")
        second = analyzer.summarize_content("the source text")
        assert first == second == "A summary."
        rows = _all_llm_calls(db)
        assert len(rows) == 1, f"expected 1 LLM call after cache hit, got {len(rows)}"

    def test_summary_fallback_is_not_cached(self, db, monkeypatch):
        """If the LLM call raises, summarize_content returns a truncated slice
        as a safety fallback. That truncation must NOT poison the cache for
        future calls — the next call with the same text should retry the LLM."""
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")

        # First call: model raises → fallback returns truncated text.
        class Boom:
            messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("rate-limited")))
        monkeypatch.setattr(analyzer, "_get_client", lambda: Boom())
        result1 = analyzer.summarize_content("x" * 200)
        assert result1.startswith("x")  # fallback truncation

        # Second call: model succeeds → fresh upstream call, not the
        # truncated fallback from before.
        fake = _FakeAnthropic(input_tokens=10, output_tokens=5, text="proper summary")
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)
        result2 = analyzer.summarize_content("x" * 200)
        assert result2 == "proper summary"


class TestOpenAICapture:
    def test_analyze_reads_openai_usage(self, db, monkeypatch):
        from bot import analyzer
        monkeypatch.setattr(analyzer, "_PROVIDER", "openai")
        monkeypatch.setattr(analyzer, "_MODEL", "gpt-4o-mini")

        fake = _FakeOpenAI(prompt_tokens=500, completion_tokens=120, content=(
            '{"main_idea":"x","why_it_matters":"y","category":"c",'
            '"quick_win":"qw","bigger_play":"bp","time_required":"5m","verdict":"skim"}'
        ))
        monkeypatch.setattr(analyzer, "_get_client", lambda: fake)

        analyzer.analyze("hi", ctx=analyzer.UsageContext(user_id=9, source_type="article"))
        rows = _all_llm_calls(db)
        assert len(rows) == 1
        assert rows[0]["provider"] == "openai"
        assert rows[0]["model"] == "gpt-4o-mini"
        assert rows[0]["input_tokens"] == 500
        assert rows[0]["output_tokens"] == 120
        # 500 in @ $0.15/MTok + 120 out @ $0.60/MTok = 0.000075 + 0.000072 = 0.000147
        assert abs(rows[0]["cost_usd"] - 0.000147) < 1e-9


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------

def _all_llm_calls(db):
    with db._get_conn() as conn:
        return conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()


class _FakeAnthropic:
    """Mimics just enough of the Anthropic client for the analyzer paths.

    For `analyze` we need a tool_use block; for `summarize_content` we need a
    text block. The constructor takes whichever the caller wants — they're
    mutually exclusive in practice.
    """
    def __init__(self, *, input_tokens, output_tokens, tool_input=None, text=None):
        if tool_input is not None:
            content = [SimpleNamespace(type="tool_use", name="record_analysis", input=tool_input)]
        else:
            content = [SimpleNamespace(type="text", text=text or "")]
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
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp))
