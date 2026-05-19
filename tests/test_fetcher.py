"""Tests for the YouTube fallback chain in bot.fetcher.

The chain (highest-quality first → cheapest-degraded last):
  youtube_transcript_api  →  yt-dlp subtitles  →  yt-dlp audio + Whisper
                                                  (only for short videos)
                                              →  description-only

These tests mock yt-dlp and youtube_transcript_api so they run offline.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"


class TestYouTubeFallbackChain:
    def test_uses_transcript_api_when_available(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi:
            TranscriptApi.return_value.fetch.return_value = [
                MagicMock(text="hello"),
                MagicMock(text="world"),
            ]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "hello world" in result["text"]
        assert result["source_type"] == "youtube"
        assert any("ytimg.com" in u for u in result.get("image_urls", []))

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
