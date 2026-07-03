"""Tests for analyzer helpers that don't require a live model.

`summarize_content` must never break persistence: empty in → empty out, and a
model error falls back to a truncated slice rather than raising.
"""
from __future__ import annotations


class TestSummarizeContent:
    def test_empty_input_returns_empty(self):
        from bot import analyzer
        assert analyzer.summarize_content("") == ""
        assert analyzer.summarize_content("   ") == ""

    def test_falls_back_to_truncation_on_model_error(self, monkeypatch):
        from bot import analyzer

        def boom():
            raise RuntimeError("model down")

        monkeypatch.setattr(analyzer, "_get_client", boom)
        # Input longer than the cap → fallback returns exactly the capped slice.
        out = analyzer.summarize_content("x" * (analyzer.SUMMARY_MAX_CHARS + 5000))
        assert out == "x" * analyzer.SUMMARY_MAX_CHARS

    def test_uses_streaming_on_anthropic(self, monkeypatch):
        """Long-transcript briefs request a large max_tokens, which the Anthropic
        SDK refuses on a *non-streaming* call ("Streaming is required for
        operations that may take longer than 10 minutes"). Summaries must go
        through `messages.stream()` so that guard never fires and silently
        drops us to the truncated-transcript fallback."""
        from bot import analyzer

        class _Block:
            type = "text"
            text = "CURATED BRIEF"

        class _FinalMessage:
            content = [_Block()]
            stop_reason = "end_turn"
            usage = type("U", (), {"input_tokens": 12, "output_tokens": 3})()

        class _StreamCtx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return _FinalMessage()

        class _Messages:
            def stream(self, **kwargs):
                return _StreamCtx()

            def create(self, **kwargs):  # pragma: no cover - must not be hit
                raise AssertionError("summarize_content must stream, not create")

        class _Client:
            messages = _Messages()

        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_get_client", lambda: _Client())

        out = analyzer.summarize_content("some long transcript text")
        assert out == "CURATED BRIEF"

    def test_output_tokens_scale_with_input(self):
        from bot import analyzer

        small = analyzer._summary_output_tokens("x" * 400)      # tiny source
        large = analyzer._summary_output_tokens("x" * 400_000)  # long source
        assert small == 512                                     # floor
        assert large == analyzer._SUMMARY_MAX_OUTPUT_TOKENS     # cap
        assert small < large


class TestNormalizeSuggestions:
    def test_keeps_and_clamps_suggestions(self):
        from bot import analyzer

        raw_suggestions = [
            {"title": f"S{i}", "detail": f"do {i}", "first_step": f"step {i}", "effort": "~1 hr"}
            for i in range(8)  # over the cap
        ]
        out = analyzer._normalize({
            "main_idea": "x", "why_it_matters": "y", "grounded_in": "g", "category": "c",
            "suggestions": raw_suggestions, "time_required": "10 min", "verdict": "watch",
        })
        assert len(out["suggestions"]) == analyzer.MAX_SUGGESTIONS  # clamped to 5
        assert out["suggestions"][0] == {"title": "S0", "detail": "do 0", "first_step": "step 0", "effort": "~1 hr"}
        assert set(out["suggestions"][0].keys()) == set(analyzer.SUGGESTION_FIELDS)

    def test_empty_suggestions_is_valid(self):
        from bot import analyzer

        out = analyzer._normalize({"verdict": "skip", "suggestions": []})
        assert out["suggestions"] == []  # 0 actions is a legitimate result

    def test_drops_empty_suggestion_rows(self):
        from bot import analyzer

        out = analyzer._normalize({"verdict": "watch", "suggestions": [
            {"title": "", "detail": "", "first_step": "x", "effort": "y"},  # dropped
            {"title": "Keep", "detail": "real", "first_step": "", "effort": ""},
        ]})
        assert [s["title"] for s in out["suggestions"]] == ["Keep"]

    def test_legacy_quick_win_synthesized_into_suggestions(self):
        from bot import analyzer

        out = analyzer._normalize({
            "main_idea": "x", "verdict": "watch",
            "quick_win": "do this in an hour", "first_step": "open file",
            "bigger_play": "the multi-week arc",
        })
        titles = [s["title"] for s in out["suggestions"]]
        assert titles == ["Quick win", "Bigger play"]
        assert out["suggestions"][0]["detail"] == "do this in an hour"
        assert out["suggestions"][0]["first_step"] == "open file"


class TestActionSchema:
    def test_scalar_fields_and_suggestions_in_schema(self):
        from bot import analyzer

        assert "grounded_in" in analyzer.ANALYSIS_FIELDS
        assert "quick_win" not in analyzer.ANALYSIS_FIELDS  # moved into suggestions[]
        assert "suggestions" in analyzer._TOOL_SCHEMA["properties"]
        assert "suggestions" in analyzer._TOOL_SCHEMA["required"]
        item = analyzer._TOOL_SCHEMA["properties"]["suggestions"]["items"]
        assert set(item["required"]) == set(analyzer.SUGGESTION_FIELDS)
