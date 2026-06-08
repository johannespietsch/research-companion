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

    def test_skips_whisper_for_long_video(self):
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
