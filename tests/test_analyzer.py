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
        # Single-part input: fallback returns the source slice itself.
        src = "x" * analyzer._SUMMARY_PART_CHARS
        assert analyzer.summarize_content(src) == src

    def test_long_source_is_briefed_part_by_part(self, monkeypatch):
        from bot import analyzer

        seen = []

        def fake_part(text, **kw):
            seen.append(len(text))
            return f"brief-{len(seen)}"

        monkeypatch.setattr(analyzer, "_summarize_part", fake_part)
        text = " ".join(["word"] * 70_000)  # ~350k chars
        out = analyzer.summarize_content(text)
        assert len(seen) == 4
        assert all(n <= analyzer._SUMMARY_PART_CHARS for n in seen)
        assert out.startswith("## Part 1 of 4\n\nbrief-1")
        assert "## Part 4 of 4\n\nbrief-4" in out

    def test_split_loses_no_words(self):
        from bot import analyzer

        text = "\n\n".join(f"para {i} " + "w " * 400 for i in range(600))
        parts = analyzer._split_for_summary(text)
        assert all(len(p) <= analyzer._SUMMARY_PART_CHARS for p in parts)
        assert " ".join(" ".join(parts).split()) == " ".join(text.split())

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
        assert small == analyzer._SUMMARY_MIN_OUTPUT_TOKENS     # floor
        assert large == analyzer._SUMMARY_MAX_OUTPUT_TOKENS     # cap
        assert small < large

    def test_short_source_gets_room_for_a_structured_brief(self):
        # Issue #128: a 4,757-char input got 1,189 tokens and the brief was
        # cut off mid-section. Short sources must get the floor, not 1:1.
        from bot import analyzer

        assert analyzer._summary_output_tokens("x" * 4_757) == 4_096
        # Above the floor, scaling is unchanged.
        assert analyzer._summary_output_tokens("x" * 40_000) == 10_000


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


class TestAnalyzeOutputLanguage:
    """The output language is decided in code from the profile, not inferred by
    the model: it followed a nationality or city mentioned in an English profile
    instead of the language the profile is written in."""

    def _prompt(self, profile: str) -> str:
        from bot import analyzer

        rule, reminder = analyzer._language_blocks(profile)
        return analyzer._PROMPT.format(
            profile_block=f"\nAbout the person you are analysing for:\n{profile}\n",
            signals_block="", text="SOURCE TEXT",
            language_rule=rule, language_reminder=reminder,
        )

    def test_english_profile_pins_english_despite_nationality_cues(self):
        prompt = self._prompt(
            "I'm a Polish AI engineer based in Lisbon. Day to day I build and "
            "evaluate LLM-powered features, maintain our retrieval pipelines and "
            "review pull requests. I want practical, concrete next steps that I "
            "can try within a week."
        )
        assert "write every field (including suggestions) in English" in prompt
        assert prompt.rstrip().endswith("whatever language the content above is in.")

    def test_german_written_profile_pins_german(self):
        prompt = self._prompt(
            "Ich bin KI-Ingenieur. Täglich baue und bewerte ich Funktionen auf Basis von "
            "LLMs, pflege unsere Retrieval-Pipelines und prüfe Pull Requests. Ich möchte "
            "konkrete Vorschläge, die ich innerhalb einer Woche ausprobieren kann."
        )
        assert "in German" in prompt
        assert "in English" not in prompt

    def test_unclear_profile_falls_back_to_prompt_only_rule(self):
        from bot import analyzer

        rule, reminder = analyzer._language_blocks("I like AI")
        assert rule == analyzer._LANGUAGE_RULE_UNKNOWN
        assert reminder == ""
        assert "write in English" in rule

    def test_prompt_formats_without_leftover_placeholders(self):
        assert "{language_" not in self._prompt("I want concise, practical suggestions for my work.")

    def test_prompt_change_invalidates_analyze_cache(self):
        from bot import analyzer

        before = analyzer._cache_key_analyze("t", "p", "m")
        original = analyzer._PROMPT
        try:
            analyzer._PROMPT = original.replace("{language_rule}", "{language_rule} ")
            assert analyzer._cache_key_analyze("t", "p", "m") != before
        finally:
            analyzer._PROMPT = original
