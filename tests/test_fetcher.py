"""Tests for the YouTube fallback chain + the URL cache in bot.fetcher.

The chain (highest-quality first → cheapest-degraded last):
  youtube_transcript_api  →  yt-dlp subtitles  →  yt-dlp audio + Whisper
                                                  (only for short videos)
                                              →  description-only

These tests mock yt-dlp and youtube_transcript_api so they run offline.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"


class TestYouTubeFallbackChain:
    def test_uses_transcript_api_when_available(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value="Real Title"):
            TranscriptApi.return_value.fetch.return_value = [
                MagicMock(text="hello"),
                MagicMock(text="world"),
            ]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "hello world" in result["text"]
        assert result["source_type"] == "youtube"
        assert any("ytimg.com" in u for u in result.get("image_urls", []))
        # Real video title surfaces instead of the "YouTube video (<id>)" placeholder.
        assert result["title"] == "Real Title"

    def test_transcript_api_title_falls_back_to_placeholder(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value=None):
            TranscriptApi.return_value.fetch.return_value = [MagicMock(text="hi")]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert result["title"] == f"YouTube video ({VIDEO_ID})"

    def test_uses_yt_dlp_subtitles_when_transcript_api_fails(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.fetch.side_effect = Exception("blocked")
            extract.return_value = {
                "text": "Title\nBy: ch\n\nTranscript:\nsubs body",
                "title": "Title",
                "source_type": "youtube",
                "has_transcript": True,
                "duration": 90,
            }

            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "subs body" in result["text"]
        assert result["source_type"] == "youtube"
        transcribe.assert_not_called()  # subtitles succeeded → no Whisper

    def test_falls_back_to_whisper_for_short_video_without_subs(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.fetch.side_effect = Exception("blocked")
            extract.return_value = {
                "text": "Title\nBy: ch\n\nshort description",
                "title": "Title",
                "source_type": "youtube",
                "has_transcript": False,
                "duration": 90,  # well under 180s
            }
            transcribe.return_value = {
                "text": "Title\nBy: ch\n\nTranscript:\nspoken words",
                "title": "Title",
                "source_type": "video",  # transcribe doesn't know it's YouTube
            }

            result = fetcher._youtube_transcript(YOUTUBE_URL)

        transcribe.assert_called_once_with(YOUTUBE_URL)
        assert "spoken words" in result["text"]
        assert result["source_type"] == "youtube"  # corrected back to youtube

    def test_skips_whisper_for_long_video(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.fetch.side_effect = Exception("blocked")
            extract.return_value = {
                "text": "Title\nBy: ch\n\nlong description",
                "title": "Title",
                "source_type": "youtube",
                "has_transcript": False,
                "duration": 1200,  # 20 min — would time out
            }

            result = fetcher._youtube_transcript(YOUTUBE_URL)

        transcribe.assert_not_called()
        assert "long description" in result["text"]

    def test_returns_description_when_whisper_fallback_fails(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.fetch.side_effect = Exception("blocked")
            extract.return_value = {
                "text": "Title\nBy: ch\n\ndescription text",
                "title": "Title",
                "source_type": "youtube",
                "has_transcript": False,
                "duration": 60,
            }
            transcribe.return_value = {"text": "", "title": "Title", "source_type": "unknown"}

            result = fetcher._youtube_transcript(YOUTUBE_URL)

        transcribe.assert_called_once()
        assert "description text" in result["text"]
        assert result["source_type"] == "youtube"

    def test_propagates_empty_extract_without_calling_whisper(self):
        """If yt-dlp extract failed entirely (e.g., 429 on metadata too),
        don't waste time on Whisper — the audio download would also fail."""
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.fetch.side_effect = Exception("blocked")
            extract.return_value = {
                "text": "",
                "title": YOUTUBE_URL,
                "source_type": "unknown",
                "has_transcript": False,
                "duration": 0,
            }

            result = fetcher._youtube_transcript(YOUTUBE_URL)

        transcribe.assert_not_called()
        assert result["text"] == ""


class TestWallDetection:
    """_wall_reason flags JS/ad-block walls and paywall teasers that return
    short boilerplate instead of article text."""

    def test_js_wall_is_flagged(self):
        from bot import fetcher, fetch_errors

        # The exact string WSJ's preference-center page returns.
        assert fetcher._wall_reason("Please enable JS and disable any ad blocker") == fetch_errors.JS_REQUIRED

    def test_paywall_teaser_is_flagged(self):
        from bot import fetcher, fetch_errors

        assert fetcher._wall_reason("Subscribe to continue reading this article.") == fetch_errors.PAYWALLED

    def test_empty_text_is_no_text_extracted(self):
        from bot import fetcher, fetch_errors

        assert fetcher._wall_reason("") == fetch_errors.NO_TEXT_EXTRACTED
        assert fetcher._wall_reason(None) == fetch_errors.NO_TEXT_EXTRACTED

    def test_short_non_wall_text_is_no_text_extracted(self):
        from bot import fetcher, fetch_errors

        assert fetcher._wall_reason("Hello there.") == fetch_errors.NO_TEXT_EXTRACTED

    def test_real_article_passes(self):
        from bot import fetcher

        article = "This is a substantive article about retrieval-augmented generation. " * 20
        assert fetcher._wall_reason(article) is None

    def test_long_article_mentioning_subscribe_is_not_a_wall(self):
        from bot import fetcher

        # A 2000+ char article that happens to contain "subscribe to continue"
        # in a footer should NOT be nuked — signatures only apply to short text.
        article = ("Detailed analysis of the topic at hand. " * 60) + " Subscribe to continue getting updates."
        assert len(article) > fetcher._WALL_SIGNATURE_MAX_CHARS
        assert fetcher._wall_reason(article) is None


class TestUrlCacheLayer:
    """`fetch_url` wraps `_fetch_url_uncached` with the url_cache."""

    def test_successful_fetch_is_cached_and_reused(self):
        from bot import fetcher

        with patch.object(fetcher, "_fetch_url_uncached", new_callable=AsyncMock) as inner:
            inner.return_value = {
                "text": "first body", "title": "Hi", "source_type": "article",
            }

            r1 = asyncio.run(fetcher.fetch_url("https://example.com/a"))
            r2 = asyncio.run(fetcher.fetch_url("https://example.com/a"))

        assert inner.call_count == 1, "second call must come from cache, not upstream"
        assert r1 == r2
        assert "first body" in r2["text"]

    def test_empty_text_is_not_cached(self):
        from bot import fetcher

        with patch.object(fetcher, "_fetch_url_uncached", new_callable=AsyncMock) as inner:
            inner.return_value = {"text": "", "title": "url", "source_type": "unknown"}

            asyncio.run(fetcher.fetch_url("https://example.com/empty"))
            asyncio.run(fetcher.fetch_url("https://example.com/empty"))

        assert inner.call_count == 2, "failed fetch shouldn't poison the cache"

    def test_different_urls_get_separate_entries(self):
        from bot import fetcher

        with patch.object(fetcher, "_fetch_url_uncached", new_callable=AsyncMock) as inner:
            def by_url(url):
                return {"text": f"body for {url}", "title": "", "source_type": "article"}
            inner.side_effect = lambda u: by_url(u)

            asyncio.run(fetcher.fetch_url("https://example.com/a"))
            asyncio.run(fetcher.fetch_url("https://example.com/b"))

            r_a2 = asyncio.run(fetcher.fetch_url("https://example.com/a"))
            r_b2 = asyncio.run(fetcher.fetch_url("https://example.com/b"))

        assert inner.call_count == 2
        assert "https://example.com/a" in r_a2["text"]
        assert "https://example.com/b" in r_b2["text"]
