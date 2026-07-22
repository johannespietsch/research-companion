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
    def _mock_transcript(self, *, language_code: str = "en", is_generated: bool = False, snippets=("hello", "world")):
        """Build a Transcript-shaped mock whose `.fetch()` yields snippets with `.text`."""
        t = MagicMock()
        t.language_code = language_code
        t.is_generated = is_generated
        t.fetch.return_value = [MagicMock(text=s) for s in snippets]
        return t

    def test_uses_transcript_api_when_available(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value="Real Title"):
            TranscriptApi.return_value.list.return_value = [
                self._mock_transcript(snippets=("hello", "world")),
            ]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "hello world" in result["text"]
        assert result["source_type"] == "youtube"
        assert any("ytimg.com" in u for u in result.get("image_urls", []))
        # Real video title surfaces instead of the "YouTube video (<id>)" placeholder.
        assert result["title"] == "Real Title"
        assert result["language"] == "en"

    def test_prefers_manual_over_auto_generated_transcript(self):
        """When manual and auto-generated transcripts exist *in the spoken
        language*, the manually created one wins — higher fidelity than ASR."""
        from bot import fetcher

        manual = self._mock_transcript(language_code="en", is_generated=False, snippets=("manual",))
        auto = self._mock_transcript(language_code="en", is_generated=True, snippets=("auto",))
        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value="t"):
            # Order shouldn't matter — manual is selected regardless.
            TranscriptApi.return_value.list.return_value = [auto, manual]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "manual" in result["text"]
        assert "auto" not in result["text"]
        assert result["language"] == "en"

    def test_picks_spoken_language_not_first_listed_manual(self):
        """Issue #57: a video with many manual *translation* tracks (Arabic
        first, alphabetically) plus an English ASR track is English-spoken — we
        must summarise the English manual track, not the first-listed Arabic."""
        from bot import fetcher

        ar = self._mock_transcript(language_code="ar", is_generated=False, snippets=("arabic",))
        en = self._mock_transcript(language_code="en", is_generated=False, snippets=("english",))
        fr = self._mock_transcript(language_code="fr", is_generated=False, snippets=("french",))
        en_auto = self._mock_transcript(language_code="en", is_generated=True, snippets=("asr",))
        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value="t"):
            TranscriptApi.return_value.list.return_value = [ar, fr, en, en_auto]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "english" in result["text"]
        assert "arabic" not in result["text"]
        assert result["language"] == "en"

    def test_anchors_on_asr_language_for_non_english_video(self):
        """A French-spoken video (French ASR) with Arabic + French manual tracks
        picks the French manual — not the first-listed Arabic, not English."""
        from bot import fetcher

        ar = self._mock_transcript(language_code="ar", is_generated=False, snippets=("arabic",))
        fr = self._mock_transcript(language_code="fr", is_generated=False, snippets=("francais",))
        fr_auto = self._mock_transcript(language_code="fr", is_generated=True, snippets=("asr",))
        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value="t"):
            TranscriptApi.return_value.list.return_value = [ar, fr, fr_auto]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "francais" in result["text"]
        assert result["language"] == "fr"

    def test_uses_non_english_auto_generated_when_thats_all_there_is(self):
        """A German-only auto-generated transcript should be picked up (regression
        for the English-default behaviour that silently dropped non-en videos)."""
        from bot import fetcher

        de_auto = self._mock_transcript(language_code="de", is_generated=True, snippets=("guten", "tag"))
        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value="t"):
            TranscriptApi.return_value.list.return_value = [de_auto]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert "guten tag" in result["text"]
        assert result["language"] == "de"

    def test_transcript_api_title_falls_back_to_placeholder(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_youtube_oembed_title", return_value=None):
            TranscriptApi.return_value.list.return_value = [self._mock_transcript(snippets=("hi",))]
            result = fetcher._youtube_transcript(YOUTUBE_URL)

        assert result["title"] == f"YouTube video ({VIDEO_ID})"

    def test_uses_yt_dlp_subtitles_when_transcript_api_fails(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.list.side_effect = Exception("blocked")
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
            TranscriptApi.return_value.list.side_effect = Exception("blocked")
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

    def test_skips_whisper_when_duration_exceeds_cap(self):
        from bot import fetcher, fetch_errors

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.list.side_effect = Exception("blocked")
            extract.return_value = {
                "text": "Title\nBy: ch\n\nlong description",
                "title": "Title",
                "source_type": "youtube",
                "has_transcript": False,
                "duration": 1200,  # 20 min
            }

            # Explicit 10-min cap → the 20-min video is over it.
            result = fetcher._youtube_transcript(
                YOUTUBE_URL, max_whisper_duration=10 * 60
            )

        transcribe.assert_not_called()
        assert "long description" in result["text"]
        assert result["reason"] == fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER

    def test_transcribes_long_video_under_signed_in_cap(self):
        """20 min is within the 2-hour signed-in cap → Whisper is attempted."""
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.list.side_effect = Exception("blocked")
            extract.return_value = {
                "text": "Title\nBy: ch\n\nlong description",
                "title": "Title",
                "source_type": "youtube",
                "has_transcript": False,
                "duration": 1200,
            }
            transcribe.return_value = {
                "text": "Title\nBy: ch\n\nTranscript:\nspoken words",
                "title": "Title",
                "source_type": "video",
            }

            result = fetcher._youtube_transcript(
                YOUTUBE_URL, max_whisper_duration=fetcher.WHISPER_MAX_DURATION_SIGNED_IN_S
            )

        transcribe.assert_called_once()
        assert "spoken words" in result["text"]

    def test_returns_description_when_whisper_fallback_fails(self):
        from bot import fetcher

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as TranscriptApi, \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            TranscriptApi.return_value.list.side_effect = Exception("blocked")
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
            TranscriptApi.return_value.list.side_effect = Exception("blocked")
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


VIMEO_URL = "https://vimeo.com/76979871"


class TestVimeoIdExtraction:
    def test_plain_url(self):
        from bot import fetcher
        assert fetcher._vimeo_id_and_hash(VIMEO_URL) == ("76979871", None)

    def test_unlisted_url_with_hash(self):
        from bot import fetcher
        assert fetcher._vimeo_id_and_hash("https://vimeo.com/76979871/abcdef0123") == (
            "76979871", "abcdef0123",
        )

    def test_channel_url(self):
        from bot import fetcher
        assert fetcher._vimeo_id_and_hash("https://vimeo.com/channels/staffpicks/76979871") == (
            "76979871", None,
        )

    def test_player_embed_url(self):
        from bot import fetcher
        assert fetcher._vimeo_id_and_hash("https://player.vimeo.com/video/76979871") == (
            "76979871", None,
        )

    def test_non_vimeo_url_returns_none(self):
        from bot import fetcher
        assert fetcher._vimeo_id_and_hash("https://example.com/path") is None


class TestVimeoFallbackChain:
    """The chain (highest-quality first → cheapest-degraded last):
      Vimeo native caption track (no duration limit)
        → yt-dlp subtitles
        → yt-dlp audio + Whisper (only for short videos)
        → description-only
    """

    def _config(self, *, tracks=None, duration=90, title="A Vimeo Video"):
        return {
            "video": {"title": title, "duration": duration, "thumbs": {"640": "https://i.vimeocdn.com/thumb.jpg"}},
            "request": {"text_tracks": tracks or []},
        }

    def test_uses_native_captions_when_available(self):
        from bot import fetcher

        config = self._config(tracks=[{"lang": "en", "url": "/texttrack/123.vtt"}])
        vtt_resp = MagicMock(text="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello world")
        vtt_resp.raise_for_status.return_value = None

        with patch.object(fetcher, "_vimeo_config", return_value=config), \
             patch.object(fetcher.requests, "get", return_value=vtt_resp) as get, \
             patch.object(fetcher, "_yt_dlp_extract") as extract:
            result = fetcher._vimeo_transcript(VIMEO_URL)

        extract.assert_not_called()
        assert "hello world" in result["text"]
        assert result["source_type"] == "video"
        assert result["transcript_source"] == "vimeo"
        assert result["language"] == "en"
        assert result["title"] == "A Vimeo Video"
        assert any("i.vimeocdn.com" in u for u in result["image_urls"])
        get.assert_called_once_with("https://player.vimeo.com/texttrack/123.vtt", timeout=15)

    def test_native_captions_bypass_whisper_duration_cap(self):
        """A long video (over max_whisper_duration) with native captions still
        gets a full transcript — the whole point of using Vimeo's own caption
        API instead of yt-dlp/Whisper for long-form videos (#101)."""
        from bot import fetcher

        config = self._config(
            tracks=[{"lang": "en", "url": "/texttrack/123.vtt"}], duration=12_000,  # ~3.3h
        )
        vtt_resp = MagicMock(text="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nlong talk content")
        vtt_resp.raise_for_status.return_value = None

        with patch.object(fetcher, "_vimeo_config", return_value=config), \
             patch.object(fetcher.requests, "get", return_value=vtt_resp), \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            result = fetcher._vimeo_transcript(VIMEO_URL, max_whisper_duration=10 * 60)

        transcribe.assert_not_called()
        assert "long talk content" in result["text"]
        assert "reason" not in result

    def test_falls_back_to_yt_dlp_when_no_native_captions(self):
        from bot import fetcher

        config = self._config(tracks=[])

        with patch.object(fetcher, "_vimeo_config", return_value=config), \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            extract.return_value = {
                "text": "Title\nBy: ch\n\nTranscript:\nsubs body",
                "title": "Title",
                "source_type": "video",
                "has_transcript": True,
                "duration": 90,
            }
            result = fetcher._vimeo_transcript(VIMEO_URL)

        assert "subs body" in result["text"]
        transcribe.assert_not_called()

    def test_falls_back_when_vimeo_config_unavailable(self):
        from bot import fetcher

        with patch.object(fetcher, "_vimeo_config", return_value=None), \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            extract.return_value = {
                "text": "Title\nBy: ch\n\nTranscript:\nsubs body",
                "title": "Title",
                "source_type": "video",
                "has_transcript": True,
                "duration": 90,
            }
            result = fetcher._vimeo_transcript(VIMEO_URL)

        assert "subs body" in result["text"]

    def test_falls_back_to_whisper_for_short_video_without_subs(self):
        from bot import fetcher

        with patch.object(fetcher, "_vimeo_config", return_value=None), \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            extract.return_value = {
                "text": "Title\nBy: ch\n\nshort description",
                "title": "Title",
                "source_type": "video",
                "has_transcript": False,
                "duration": 90,
            }
            transcribe.return_value = {
                "text": "Title\nBy: ch\n\nTranscript:\nspoken words",
                "title": "Title",
                "source_type": "video",
            }
            result = fetcher._vimeo_transcript(VIMEO_URL)

        transcribe.assert_called_once_with(VIMEO_URL)
        assert "spoken words" in result["text"]
        assert result["transcript_source"] == "whisper"

    def test_skips_whisper_when_duration_exceeds_cap_and_no_captions(self):
        from bot import fetcher, fetch_errors

        with patch.object(fetcher, "_vimeo_config", return_value=None), \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            extract.return_value = {
                "text": "Title\nBy: ch\n\nlong description",
                "title": "Title",
                "source_type": "video",
                "has_transcript": False,
                "duration": 1200,
            }
            result = fetcher._vimeo_transcript(VIMEO_URL, max_whisper_duration=10 * 60)

        transcribe.assert_not_called()
        assert result["reason"] == fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER

    def test_propagates_empty_extract_without_calling_whisper(self):
        from bot import fetcher

        with patch.object(fetcher, "_vimeo_config", return_value=None), \
             patch.object(fetcher, "_yt_dlp_extract") as extract, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            extract.return_value = {
                "text": "",
                "title": VIMEO_URL,
                "source_type": "unknown",
                "has_transcript": False,
                "duration": 0,
            }
            result = fetcher._vimeo_transcript(VIMEO_URL)

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

    def test_too_long_for_whisper_is_not_cached(self):
        """The one tier-dependent degraded outcome must not be cached, or a
        stricter (anon) tier would poison a more generous (signed-in) one."""
        from bot import fetcher, fetch_errors

        with patch.object(fetcher, "_fetch_url_uncached", new_callable=AsyncMock) as inner:
            inner.return_value = {
                "text": "Title\nBy: ch",  # title-only stub
                "title": "Title",
                "source_type": "youtube",
                "reason": fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER,
            }

            asyncio.run(fetcher.fetch_url("https://youtube.com/watch?v=zzzzzzzzzzz"))
            asyncio.run(fetcher.fetch_url("https://youtube.com/watch?v=zzzzzzzzzzz"))

        assert inner.call_count == 2, "too-long result must not be cached"

    def test_different_urls_get_separate_entries(self):
        from bot import fetcher

        with patch.object(fetcher, "_fetch_url_uncached", new_callable=AsyncMock) as inner:
            def by_url(url, **kwargs):
                return {"text": f"body for {url}", "title": "", "source_type": "article"}
            inner.side_effect = lambda u, **kw: by_url(u, **kw)

            asyncio.run(fetcher.fetch_url("https://example.com/a"))
            asyncio.run(fetcher.fetch_url("https://example.com/b"))

            r_a2 = asyncio.run(fetcher.fetch_url("https://example.com/a"))
            r_b2 = asyncio.run(fetcher.fetch_url("https://example.com/b"))

        assert inner.call_count == 2
        assert "https://example.com/a" in r_a2["text"]
        assert "https://example.com/b" in r_b2["text"]

    def test_skip_cache_bypasses_read_but_refreshes_entry(self):
        """A stale cached fetch (e.g. from a since-fixed extraction bug) must
        stay bypassable on demand — see issue #103's retrigger endpoint."""
        from bot import fetcher

        with patch.object(fetcher, "_fetch_url_uncached", new_callable=AsyncMock) as inner:
            inner.side_effect = [
                {"text": "stale body", "title": "Hi", "source_type": "article"},
                {"text": "fresh body", "title": "Hi", "source_type": "article"},
            ]

            r1 = asyncio.run(fetcher.fetch_url("https://example.com/a"))
            r2 = asyncio.run(fetcher.fetch_url("https://example.com/a", skip_cache=True))
            # A subsequent normal (cached) read now sees the refreshed entry.
            r3 = asyncio.run(fetcher.fetch_url("https://example.com/a"))

        assert inner.call_count == 2, "skip_cache must hit upstream, not the stale entry"
        assert r1["text"] == "stale body"
        assert r2["text"] == "fresh body"
        assert r3["text"] == "fresh body", "skip_cache run must refresh the cache entry"


class TestRobots:
    """robots.txt is deliberately NOT consulted: a generic fetch is a single
    user-initiated request (a user agent acting for the person), not crawling.
    The page is fetched regardless of what robots.txt would say."""

    def test_generic_path_does_not_consult_robots(self, monkeypatch):
        from bot import fetcher

        monkeypatch.setattr(fetcher, "assert_public_url", lambda u: None)  # skip DNS
        called = {"generic": False}

        async def _generic(u):
            called["generic"] = True
            return {"text": "article body", "title": "T", "source_type": "article"}

        monkeypatch.setattr(fetcher, "_generic_fetch", _generic)
        result = asyncio.run(fetcher._fetch_url_uncached("https://example.com/post"))
        assert called["generic"] is True, "user-initiated fetch must proceed"
        assert result["text"] == "article body"
        assert "reason" not in result

    def test_robots_machinery_is_gone(self):
        """No leftover robots gate that a future change could re-wire."""
        from bot import fetch_errors, fetcher

        assert not hasattr(fetcher, "_robots_allows")
        assert not hasattr(fetcher, "_robots_parser_for")
        assert not hasattr(fetch_errors, "BLOCKED_BY_ROBOTS")


class TestDOIExtraction:
    def test_springer_article_url(self):
        from bot import fetcher
        assert fetcher._extract_doi(
            "https://link.springer.com/article/10.1007/s00146-026-03095-6"
        ) == "10.1007/s00146-026-03095-6"

    def test_doi_org_url(self):
        from bot import fetcher
        assert fetcher._extract_doi("https://doi.org/10.1086/681238") == "10.1086/681238"

    def test_trailing_page_suffix_is_trimmed(self):
        from bot import fetcher
        assert fetcher._extract_doi(
            "https://onlinelibrary.wiley.com/doi/10.1111/jne.12345/full"
        ) == "10.1111/jne.12345"

    def test_query_string_is_ignored(self):
        from bot import fetcher
        assert fetcher._extract_doi(
            "https://example.com/article/10.1007/abc123?utm_source=x"
        ) == "10.1007/abc123"

    def test_non_academic_url_returns_none(self):
        from bot import fetcher
        assert fetcher._extract_doi("https://example.com/blog/some-post") is None


class TestJatsAndAuthors:
    def test_strip_jats_removes_tags_and_heading(self):
        from bot import fetcher
        raw = "<jats:title>Abstract</jats:title><jats:p>Hello &amp; welcome.</jats:p>"
        assert fetcher._strip_jats(raw) == "Hello & welcome."

    def test_strip_jats_empty(self):
        from bot import fetcher
        assert fetcher._strip_jats("") == ""

    def test_format_authors(self):
        from bot import fetcher
        authors = [{"given": "Ada", "family": "Lovelace"}, {"family": "Turing"}]
        assert fetcher._format_authors(authors) == "Ada Lovelace, Turing"

    def test_format_authors_non_list(self):
        from bot import fetcher
        assert fetcher._format_authors(None) == ""


class TestCrossref:
    def _resp(self, status, payload):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        return r

    def test_returns_title_abstract_authors(self, monkeypatch):
        from bot import fetcher
        payload = {
            "message": {
                "title": ["On Computable Numbers"],
                "abstract": "<jats:p>A study of decidability.</jats:p>",
                "author": [{"given": "Alan", "family": "Turing"}],
            }
        }
        monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: self._resp(200, payload))
        meta = fetcher._crossref_fetch("10.1112/plms/s2-42.1.230")
        assert meta["title"] == "On Computable Numbers"
        assert meta["abstract"] == "A study of decidability."
        assert meta["authors"] == "Alan Turing"

    def test_non_200_returns_none(self, monkeypatch):
        from bot import fetcher
        monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: self._resp(404, {}))
        assert fetcher._crossref_fetch("10.0/nope") is None


class TestAcademicFetch:
    def test_prefers_unpaywall_oa_full_text(self, monkeypatch):
        from bot import fetcher

        monkeypatch.setattr(fetcher, "_unpaywall_oa_url", lambda doi: "https://repo.org/paper.pdf")
        monkeypatch.setattr(fetcher, "assert_public_url", lambda u: None)

        async def _pdf(u):
            return {"text": "full open-access body", "title": "Paper", "source_type": "pdf"}

        monkeypatch.setattr(fetcher, "_pdf_fetch", _pdf)
        # Crossref shouldn't be needed when OA full text is found.
        monkeypatch.setattr(fetcher, "_crossref_fetch", lambda doi: (_ for _ in ()).throw(AssertionError("should not call")))

        result = asyncio.run(fetcher._academic_fetch("https://pub/10.1/x", "10.1/x"))
        assert result["text"] == "full open-access body"
        assert result["source_type"] == "academic"

    def test_falls_back_to_crossref_abstract(self, monkeypatch):
        from bot import fetcher

        monkeypatch.setattr(fetcher, "_unpaywall_oa_url", lambda doi: None)
        monkeypatch.setattr(
            fetcher, "_crossref_fetch",
            lambda doi: {"title": "Walled Paper", "abstract": "The abstract.", "authors": "A. Author"},
        )
        result = asyncio.run(fetcher._academic_fetch("https://pub/10.1/x", "10.1/x"))
        assert result["source_type"] == "academic"
        assert result["title"] == "Walled Paper"
        assert "The abstract." in result["text"]
        assert "By: A. Author" in result["text"]

    def test_returns_none_when_nothing_found(self, monkeypatch):
        from bot import fetcher
        monkeypatch.setattr(fetcher, "_unpaywall_oa_url", lambda doi: None)
        monkeypatch.setattr(fetcher, "_crossref_fetch", lambda doi: None)
        assert asyncio.run(fetcher._academic_fetch("https://pub/10.1/x", "10.1/x")) is None

    def test_oa_url_blocked_falls_through_to_abstract(self, monkeypatch):
        from bot import fetcher
        from bot.ssrf import BlockedURLError

        monkeypatch.setattr(fetcher, "_unpaywall_oa_url", lambda doi: "http://169.254.169.254/x")

        def _block(u):
            raise BlockedURLError("private")

        monkeypatch.setattr(fetcher, "assert_public_url", _block)
        monkeypatch.setattr(fetcher, "_crossref_fetch", lambda doi: {"title": "P", "abstract": "Safe abstract.", "authors": ""})
        result = asyncio.run(fetcher._academic_fetch("https://pub/10.1/x", "10.1/x"))
        assert "Safe abstract." in result["text"]


class TestArticleAcademicWiring:
    def test_walled_article_with_doi_uses_academic_fallback(self, monkeypatch):
        from bot import fetcher

        monkeypatch.setattr(fetcher, "assert_public_url", lambda u: None)

        async def _walled(u):
            return {"text": "", "title": u, "source_type": "article", "reason": fetcher.fetch_errors.JS_REQUIRED}

        async def _academic(url, doi):
            return {"text": "abstract body", "title": "T", "source_type": "academic"}

        monkeypatch.setattr(fetcher, "_generic_fetch", _walled)
        monkeypatch.setattr(fetcher, "_academic_fetch", _academic)
        result = asyncio.run(
            fetcher._fetch_url_uncached("https://link.springer.com/article/10.1007/s00146-026-03095-6")
        )
        assert result["source_type"] == "academic"
        assert result["text"] == "abstract body"

    def test_good_extract_does_not_trigger_academic(self, monkeypatch):
        from bot import fetcher

        monkeypatch.setattr(fetcher, "assert_public_url", lambda u: None)

        async def _good(u):
            return {"text": "a real full article body", "title": "T", "source_type": "article"}

        monkeypatch.setattr(fetcher, "_generic_fetch", _good)
        monkeypatch.setattr(
            fetcher, "_academic_fetch",
            lambda url, doi: (_ for _ in ()).throw(AssertionError("must not call academic on good extract")),
        )
        result = asyncio.run(
            fetcher._fetch_url_uncached("https://link.springer.com/article/10.1007/s00146-026-03095-6")
        )
        assert result["text"] == "a real full article body"

    def test_walled_article_without_doi_keeps_wall(self, monkeypatch):
        from bot import fetcher

        monkeypatch.setattr(fetcher, "assert_public_url", lambda u: None)

        async def _walled(u):
            return {"text": "", "title": u, "source_type": "article", "reason": fetcher.fetch_errors.PAYWALLED}

        monkeypatch.setattr(fetcher, "_generic_fetch", _walled)
        result = asyncio.run(fetcher._fetch_url_uncached("https://nytimes.com/2026/some-story"))
        assert result["reason"] == fetcher.fetch_errors.PAYWALLED


class TestDirectAudioUrls:
    """Direct audio (podcast) URLs route to transcription, not the HTML path (#8)."""

    def test_looks_like_audio_by_suffix(self):
        from bot.fetcher import _looks_like_audio
        assert _looks_like_audio("https://cdn.example/ep1.mp3")
        assert _looks_like_audio("https://cdn.example/ep1.m4a?token=abc")
        # anchor.fm-style play link carries the real .mp3 at the path's end
        assert _looks_like_audio(
            "https://anchor.fm/s/abc/podcast/play/123/"
            "https%3A%2F%2Fd3.cloudfront.net%2Fstaging%2Fx-44100-2-y.mp3"
        )
        assert not _looks_like_audio("https://example.com/article")
        assert not _looks_like_audio("https://example.com/listen?file=ep.mp3")

    def test_fetch_url_routes_audio_to_transcriber(self, monkeypatch):
        from bot import fetcher
        monkeypatch.setattr(fetcher, "assert_public_url", lambda u: None)
        with patch.object(fetcher, "_transcribe_audio_url") as transcribe, \
             patch.object(fetcher, "_generic_fetch", new_callable=AsyncMock) as generic:
            transcribe.return_value = {"text": "Episode\n\nTranscript:\nhello",
                                       "title": "Episode", "source_type": "audio"}
            result = asyncio.run(fetcher._fetch_url_uncached("https://cdn.example/ep1.mp3"))
        transcribe.assert_called_once()
        generic.assert_not_called()
        assert result["source_type"] == "audio"
        assert "hello" in result["text"]

    def test_transcribe_audio_url_skips_whisper_when_too_long(self):
        from bot import fetcher, fetch_errors
        with patch("yt_dlp.YoutubeDL") as YDL, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            ydl = YDL.return_value.__enter__.return_value
            ydl.extract_info.return_value = {"title": "Long Pod", "duration": 9000}  # 2.5h
            result = fetcher._transcribe_audio_url("https://cdn.example/ep.mp3",
                                                   max_whisper_duration=2 * 3600)
        transcribe.assert_not_called()
        assert result["reason"] == fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
        assert result["source_type"] == "audio"

    def test_transcribe_audio_url_proceeds_when_duration_unknown(self):
        from bot import fetcher
        with patch("yt_dlp.YoutubeDL") as YDL, \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            ydl = YDL.return_value.__enter__.return_value
            ydl.extract_info.return_value = {"title": "Pod"}  # no duration
            transcribe.return_value = {"text": "t", "title": "Pod", "source_type": "video"}
            result = fetcher._transcribe_audio_url("https://cdn.example/ep.mp3",
                                                   max_whisper_duration=1800)
        transcribe.assert_called_once_with("https://cdn.example/ep.mp3")
        assert result["source_type"] == "audio"  # corrected from transcribe's "video"


class TestRedditFetch:
    """Reddit www/app pages are a JS shell; route to old.reddit and never let a
    blocked/removed page become a verdict (#67)."""

    def test_rewrites_www_to_old_reddit(self):
        from bot.fetcher import _to_old_reddit
        assert _to_old_reddit(
            "https://www.reddit.com/r/x/comments/abc/title/"
        ) == "https://old.reddit.com/r/x/comments/abc/title/"
        assert _to_old_reddit("https://reddit.com/r/x/comments/abc/t/") \
            == "https://old.reddit.com/r/x/comments/abc/t/"
        # media + already-old hosts are left untouched
        assert _to_old_reddit("https://i.redd.it/abc.png") == "https://i.redd.it/abc.png"
        assert _to_old_reddit("https://old.reddit.com/r/x/") == "https://old.reddit.com/r/x/"

    def test_fetch_url_routes_reddit_through_old_host(self, monkeypatch):
        from bot import fetcher
        monkeypatch.setattr(fetcher, "assert_public_url", lambda u: None)
        seen = {}

        async def fake_generic(u):
            seen["url"] = u
            return {"text": "real post body " * 30, "title": "Post", "source_type": "article"}

        monkeypatch.setattr(fetcher, "_generic_fetch", fake_generic)
        result = asyncio.run(
            fetcher._fetch_url_uncached("https://www.reddit.com/r/x/comments/abc/title/")
        )
        assert seen["url"].startswith("https://old.reddit.com/")
        assert result["text"].startswith("real post body")

    def test_blocked_reddit_page_is_no_content_not_a_verdict(self):
        # The "blocked / does not exist" boilerplate must read as no-content so
        # the pipeline errors instead of analysing chrome into a verdict.
        from bot import fetcher, fetch_errors
        blocked = "You've been blocked by network security. File a ticket below."
        removed = "the page you requested does not exist. " + "x " * 100  # > min chars
        assert fetcher._wall_reason(blocked) == fetch_errors.NO_TEXT_EXTRACTED
        assert fetcher._wall_reason(removed) == fetch_errors.NO_TEXT_EXTRACTED
        # a genuine long post is still real content
        assert fetcher._wall_reason("A substantive discussion. " * 50) is None

    def test_reddit_failure_message_is_specific(self):
        from bot.fetch_errors import user_message, NO_TEXT_EXTRACTED, JS_REQUIRED
        msg = user_message(NO_TEXT_EXTRACTED, "https://www.reddit.com/r/x/comments/abc/t/")
        assert "Reddit" in msg and "paste" in msg.lower()
        # non-reddit keeps the generic message
        assert "Reddit" not in user_message(JS_REQUIRED, "https://example.com/a")


class TestXArticleExtraction:
    # fxtwitter puts the X Article body in article.content.blocks (Draft.js
    # style), not article.text — that key doesn't exist in current responses.
    # See issue #103: articles were coming through as title + attribution only.

    def _article_tweet(self, blocks=None, preview_text="preview only"):
        return {
            "author": {"screen_name": "gregisenberg", "name": "GREG ISENBERG"},
            "text": "",
            "article": {
                "title": "How I'd make $10 million with AI agents",
                "preview_text": preview_text,
                "content": {"blocks": blocks} if blocks is not None else {},
            },
        }

    def test_article_body_is_reconstructed_from_blocks(self):
        from bot import fetcher
        tweet = self._article_tweet(blocks=[
            {"text": "First paragraph.", "type": "unstyled"},
            {"text": "", "type": "unstyled"},  # empty blocks are dropped
            {"text": "Second paragraph.", "type": "header-two"},
        ])
        result = fetcher._format_fxtwitter(tweet, "https://x.com/i/status/1")
        assert "First paragraph." in result["text"]
        assert "Second paragraph." in result["text"]
        assert result["title"] == "How I'd make $10 million with AI agents"

    def test_falls_back_to_preview_text_when_no_blocks(self):
        from bot import fetcher
        tweet = self._article_tweet(blocks=[], preview_text="just the preview")
        result = fetcher._format_fxtwitter(tweet, "https://x.com/i/status/1")
        assert "just the preview" in result["text"]

    def test_regular_tweet_without_article_is_unaffected(self):
        from bot import fetcher
        tweet = {
            "author": {"screen_name": "someone", "name": "Someone"},
            "text": "just a normal tweet",
        }
        result = fetcher._format_fxtwitter(tweet, "https://x.com/i/status/1")
        assert "just a normal tweet" in result["text"]
        assert result["title"] == "Post by @someone"


TWEET_URL = "https://x.com/someone/status/1234567890"


class TestTweetVideoTranscript:
    """A tweet with an embedded video should get a Whisper transcript appended
    to its caption (mirroring the YouTube/Vimeo captionless-video tier) —
    otherwise a 2h video with a 3-sentence caption summarises off the caption
    alone (issue reported: '~300 word summary, only the post's text was used')."""

    def _tweet(self, *, caption="Check out this video", duration=90, screen_name="someone"):
        return {
            "author": {"screen_name": screen_name, "name": "Someone"},
            "text": caption,
            "media": {
                "videos": [{"url": "https://video.twimg.com/vid.mp4", "duration": duration}],
            },
        }

    def test_photo_only_tweet_is_unaffected(self):
        from bot import fetcher

        tweet = {"author": {"screen_name": "someone", "name": "Someone"}, "text": "just text"}
        formatted = fetcher._format_fxtwitter(tweet, TWEET_URL)
        with patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            result = fetcher._tweet_video_transcript(formatted, tweet, TWEET_URL, 30 * 60)

        transcribe.assert_not_called()
        assert result is formatted
        assert "reason" not in result

    def test_short_video_gets_transcribed_and_appended_to_caption(self):
        from bot import fetcher

        tweet = self._tweet(duration=90)
        formatted = fetcher._format_fxtwitter(tweet, TWEET_URL)

        with patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            transcribe.return_value = {
                "text": "Title\nBy: someone\n\nTranscript:\nspoken content here",
                "transcript": "spoken content here",
                "title": "Title",
                "source_type": "video",
            }
            result = fetcher._tweet_video_transcript(formatted, tweet, TWEET_URL, 30 * 60)

        transcribe.assert_called_once_with(TWEET_URL)
        assert "Check out this video" in result["text"]
        assert "spoken content here" in result["text"]
        assert result["transcript_source"] == "whisper"
        assert result["duration"] == 90

    def test_video_longer_than_cap_is_skipped_but_caption_kept(self):
        from bot import fetcher, fetch_errors

        tweet = self._tweet(duration=3 * 60 * 60)  # 3h, over the 30min anon cap
        formatted = fetcher._format_fxtwitter(tweet, TWEET_URL)

        with patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            result = fetcher._tweet_video_transcript(formatted, tweet, TWEET_URL, 30 * 60)

        transcribe.assert_not_called()
        assert result["reason"] == fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
        assert "Check out this video" in result["text"]

    def test_whisper_failure_keeps_caption_and_flags_reason(self):
        from bot import fetcher, fetch_errors

        tweet = self._tweet(duration=90)
        formatted = fetcher._format_fxtwitter(tweet, TWEET_URL)

        with patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            transcribe.return_value = {"text": "", "title": TWEET_URL, "source_type": "unknown"}
            result = fetcher._tweet_video_transcript(formatted, tweet, TWEET_URL, 30 * 60)

        assert result["reason"] == fetch_errors.WHISPER_FAILED
        assert "Check out this video" in result["text"]

    def test_fetch_tweet_wires_video_transcript_through_fxtwitter_tier(self):
        """End-to-end through `_fetch_tweet`: fxtwitter tier 1 result flows
        into the video-transcript step automatically."""
        from bot import fetcher

        tweet = self._tweet(duration=90)
        with patch.object(fetcher, "_fxtwitter_fetch", return_value=tweet), \
             patch.object(fetcher, "_yt_dlp_transcribe") as transcribe:
            transcribe.return_value = {
                "text": "Title\n\nTranscript:\nfull spoken text",
                "transcript": "full spoken text",
            }
            result = fetcher._fetch_tweet(TWEET_URL, max_whisper_duration=30 * 60)

        assert "full spoken text" in result["text"]
        assert result["transcript_source"] == "whisper"
