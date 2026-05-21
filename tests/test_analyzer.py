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

    def test_output_tokens_scale_with_input(self):
        from bot import analyzer

        small = analyzer._summary_output_tokens("x" * 400)      # tiny source
        large = analyzer._summary_output_tokens("x" * 400_000)  # long source
        assert small == 512                                     # floor
        assert large == analyzer._SUMMARY_MAX_OUTPUT_TOKENS     # cap
        assert small < large
