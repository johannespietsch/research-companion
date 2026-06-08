"""Tests for bot/pipeline.py — the unified URL → analysis chain that every
URL-based entry point goes through (web /api/try, /api/job, /api/library/add,
/submit/url, and Telegram URL handler).

The whole point of the pipeline is that the *same* input produces the *same*
analyze() call regardless of which entry point. These tests pin that
contract: same URL → same analyze input → cache hits work end-to-end."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def pipeline(monkeypatch):
    """Mock the pipeline's external dependencies with deterministic fakes.

    Tests override these via further monkeypatch calls when they need to
    exercise a specific edge case (empty text, video source_type, etc.)."""
    import bot.pipeline

    async def fake_fetch_url(url):
        return {
            "text": "Long article body about retrieval-augmented generation.",
            "title": "RAG explained",
            "source_type": "article",
            "image_urls": [],
        }

    def fake_summarize_content(text, **_kwargs):
        return f"SUMMARY({len(text)} chars)"

    def fake_analyze(text, user_id=None, **_kwargs):
        return {
            "main_idea": text,  # echo the input so we can assert what was analyzed
            "why_it_matters": "",
            "category": "",
            "quick_win": "",
            "bigger_play": "",
            "time_required": "",
            "verdict": "watch",
        }

    monkeypatch.setattr(bot.pipeline, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(bot.pipeline, "summarize_content", fake_summarize_content)
    monkeypatch.setattr(bot.pipeline, "analyze", fake_analyze)
    return bot.pipeline


class TestAnalyzeUrlBasics:
    def test_returns_pipeline_result(self, pipeline):
        from bot.analyzer import UsageContext
        result = asyncio.run(pipeline.analyze_url(
            "https://example.com/post", ctx=UsageContext()
        ))
        assert result.title == "RAG explained"
        assert result.source_type == "article"
        assert result.summary.startswith("SUMMARY(")
        assert result.analysis["verdict"] == "watch"

    def test_analyze_runs_on_summary_not_raw_text(self, pipeline):
        """Canonical-representation contract: analyze sees the SUMMARY,
        not the raw fetched text. This is what makes the cache key stable
        for long content where the raw text varies slightly between fetches."""
        from bot.analyzer import UsageContext
        result = asyncio.run(pipeline.analyze_url(
            "https://example.com/post", ctx=UsageContext()
        ))
        # fake_analyze echoes its input via main_idea
        assert result.analysis["main_idea"].startswith("SUMMARY(")

    def test_signed_in_save_persists_item(self, pipeline, db):
        from bot.analyzer import UsageContext
        uid = db.get_or_create_user_by_telegram(1)
        result = asyncio.run(pipeline.analyze_url(
            "https://example.com/post",
            ctx=UsageContext(user_id=uid),
            save_for_user_id=uid,
            user_note="a note",
        ))
        assert result.saved_id is not None
        rows = db.get_all_items(uid)
        assert len(rows) == 1
        assert rows[0]["source"] == "https://example.com/post"
        assert rows[0]["source_type"] == "article"
        assert rows[0]["user_note"] == "a note"

    def test_anonymous_does_not_save(self, pipeline, db):
        from bot.analyzer import UsageContext
        result = asyncio.run(pipeline.analyze_url(
            "https://example.com/post",
            ctx=UsageContext(anon_id="anon-1"),
        ))
        assert result.saved_id is None


class TestErrors:
    def test_fetch_failure_raises_pipeline_error(self, pipeline, monkeypatch):
        async def boom_fetch(url):
            raise RuntimeError("network down")
        monkeypatch.setattr(pipeline, "fetch_url", boom_fetch)

        from bot.analyzer import UsageContext
        with pytest.raises(pipeline.PipelineError) as exc_info:
            asyncio.run(pipeline.analyze_url("x", ctx=UsageContext()))
        assert exc_info.value.code == pipeline.ERR_FETCH_FAILED

    def test_empty_text_for_article_raises_extraction_failed(
        self, pipeline, monkeypatch
    ):
        async def empty_fetch(url):
            return {"text": "", "title": url, "source_type": "article",
                    "image_urls": [], "reason": "no_text"}
        monkeypatch.setattr(pipeline, "fetch_url", empty_fetch)

        from bot.analyzer import UsageContext
        with pytest.raises(pipeline.PipelineError) as exc_info:
            asyncio.run(pipeline.analyze_url("x", ctx=UsageContext()))
        assert exc_info.value.code == pipeline.ERR_NO_TEXT
        assert exc_info.value.fetched["reason"] == "no_text"

    def test_empty_text_for_video_raises_no_transcript(
        self, pipeline, monkeypatch
    ):
        async def empty_video(url):
            return {"text": "", "title": url, "source_type": "youtube",
                    "image_urls": [], "reason": "no_transcript"}
        monkeypatch.setattr(pipeline, "fetch_url", empty_video)

        from bot.analyzer import UsageContext
        with pytest.raises(pipeline.PipelineError) as exc_info:
            asyncio.run(pipeline.analyze_url("x", ctx=UsageContext()))
        assert exc_info.value.code == pipeline.ERR_NO_TRANSCRIPT

    def test_thin_stub_with_reason_for_video_raises_no_transcript(
        self, pipeline, monkeypatch
    ):
        # Captionless video, too long for Whisper, empty description: the
        # fetcher returns a title-only stub *plus* a reason. We must surface
        # the reason, not analyse the stub.
        async def thin_video(url):
            return {"text": "Real Boom? Fake Money?\nBy: THE JACK MALLERS SHOW",
                    "title": "Real Boom? Fake Money?", "source_type": "youtube",
                    "image_urls": [], "reason": "video_too_long_for_whisper"}
        monkeypatch.setattr(pipeline, "fetch_url", thin_video)

        from bot.analyzer import UsageContext
        with pytest.raises(pipeline.PipelineError) as exc_info:
            asyncio.run(pipeline.analyze_url("x", ctx=UsageContext()))
        assert exc_info.value.code == pipeline.ERR_NO_TRANSCRIPT
        assert exc_info.value.fetched["reason"] == "video_too_long_for_whisper"

    def test_short_description_without_reason_still_analyses(
        self, pipeline, monkeypatch
    ):
        # A successful (unflagged) fetch with short text must NOT be gated —
        # only degraded fetches carrying a `reason` are subject to the floor.
        async def short_ok(url):
            return {"text": "A short but real article.", "title": "t",
                    "source_type": "article", "image_urls": []}
        monkeypatch.setattr(pipeline, "fetch_url", short_ok)

        from bot.analyzer import UsageContext
        result = asyncio.run(pipeline.analyze_url("x", ctx=UsageContext()))
        assert result.analysis is not None

    def test_analyze_crash_raises_analyze_failed(self, pipeline, monkeypatch):
        def boom(_text, **_kwargs):
            raise RuntimeError("rate limited")
        monkeypatch.setattr(pipeline, "analyze", boom)

        from bot.analyzer import UsageContext
        with pytest.raises(pipeline.PipelineError) as exc_info:
            asyncio.run(pipeline.analyze_url("x", ctx=UsageContext()))
        assert exc_info.value.code == pipeline.ERR_ANALYZE_FAILED
        # fetched is attached so callers can include title in error UX
        assert exc_info.value.fetched["title"] == "RAG explained"


class TestImageStrategy:
    """The image-description step is what made Telegram diverge before. Now
    it's a per-source-type default with an explicit override. These tests
    pin the matrix so changes are deliberate."""

    @pytest.fixture
    def with_images(self, pipeline, monkeypatch):
        """Override fetch to return image_urls; capture analyze input to
        verify image descriptions did/didn't end up there."""
        async def fetch_with_images(url):
            return {
                "text": "Article body",
                "title": "T",
                "source_type": "article",
                "image_urls": ["https://example.com/img1.jpg"],
            }
        monkeypatch.setattr(pipeline, "fetch_url", fetch_with_images)

        captured: dict[str, Any] = {}

        async def fake_describe(image_urls, *, ctx):
            return "\n\nIMAGE DESCRIPTIONS:\n[Image 1]: a chart"

        def capture_summarize(text, **_kw):
            captured["summary_input"] = text
            return f"SUMMARY({len(text)} chars)"

        monkeypatch.setattr(pipeline, "_describe_images", fake_describe)
        monkeypatch.setattr(pipeline, "summarize_content", capture_summarize)
        return pipeline, captured

    def test_article_includes_images_by_default(self, with_images):
        pl, captured = with_images
        from bot.analyzer import UsageContext
        asyncio.run(pl.analyze_url("x", ctx=UsageContext()))
        assert "IMAGE DESCRIPTIONS" in captured["summary_input"]

    def test_youtube_excludes_images_by_default(self, with_images, monkeypatch):
        pl, captured = with_images

        async def fetch_youtube(url):
            return {
                "text": "Transcript here",
                "title": "T",
                "source_type": "youtube",
                "image_urls": ["https://yt/thumbnail.jpg"],
            }
        monkeypatch.setattr(pl, "fetch_url", fetch_youtube)

        from bot.analyzer import UsageContext
        asyncio.run(pl.analyze_url("x", ctx=UsageContext()))
        assert "IMAGE DESCRIPTIONS" not in captured["summary_input"]

    def test_explicit_false_overrides_default(self, with_images):
        pl, captured = with_images
        from bot.analyzer import UsageContext
        asyncio.run(pl.analyze_url("x", ctx=UsageContext(), include_images=False))
        assert "IMAGE DESCRIPTIONS" not in captured["summary_input"]


class TestProgressCallback:
    def test_on_step_called_with_known_labels(self, pipeline):
        steps: list[str] = []

        from bot.analyzer import UsageContext
        asyncio.run(pipeline.analyze_url(
            "x", ctx=UsageContext(), on_step=steps.append,
        ))
        # Article with no images skips describing-images.
        assert steps == ["fetching", "summarizing", "analyzing"]

    def test_on_step_failure_does_not_break_pipeline(self, pipeline):
        def bad_callback(_label):
            raise RuntimeError("logger crashed")

        from bot.analyzer import UsageContext
        result = asyncio.run(pipeline.analyze_url(
            "x", ctx=UsageContext(), on_step=bad_callback,
        ))
        assert result.analysis["verdict"] == "watch"  # ran to completion
