import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import requests

from bot import fetch_errors
from bot.config import MAX_CONTENT_CHARS

logger = logging.getLogger(__name__)

_YT_PATTERNS = re.compile(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})")


def _youtube_thumbnail(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _youtube_oembed_title(url: str) -> str | None:
    """Fetch a video's real title via YouTube's public oEmbed endpoint.

    The youtube_transcript_api path gives us the transcript but no metadata, so
    without this we fall back to a "YouTube video (<id>)" placeholder. oEmbed is
    free, unauthenticated, and hits a different endpoint than the transcript API
    (so it rarely shares its rate limits) — much cheaper than a full yt-dlp
    metadata pass just to recover the title.
    """
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
            headers={"User-Agent": "research-companion-bot/1.0"},
        )
        if resp.status_code != 200:
            return None
        title = resp.json().get("title")
        return title.strip() if isinstance(title, str) and title.strip() else None
    except Exception as e:
        logger.warning(f"YouTube oEmbed title fetch failed for {url}: {e}")
        return None


# Whisper fallback is only attempted for videos short enough that the
# audio download + transcription has a real chance of completing within the
# Worker's 25 s timeout on `/api/try` / `/api/library/add`. Long videos fall
# back to title + uploader + description.
_WHISPER_FALLBACK_MAX_DURATION_S = 180


def _youtube_transcript(url: str) -> dict:
    from youtube_transcript_api import YouTubeTranscriptApi

    match = _YT_PATTERNS.search(url)
    if not match:
        return _yt_dlp_extract(url)

    video_id = match.group(1)
    thumb = _youtube_thumbnail(video_id)
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
        text = " ".join(snippet.text for snippet in fetched)
        title = _youtube_oembed_title(url) or f"YouTube video ({video_id})"
        return {
            "text": f"{title}\n\nTranscript:\n{text}"[:MAX_CONTENT_CHARS],
            "title": title,
            "source_type": "youtube",
            "image_urls": [thumb],
        }
    except Exception:
        logger.info(f"No transcript for {video_id}, falling back to yt-dlp")

    extract = _yt_dlp_extract(url)
    extract.setdefault("image_urls", []).append(thumb)

    # If yt-dlp also got us a transcript (or extraction died entirely), use what
    # we have. The Whisper branch only kicks in when we're left with a
    # description-only answer AND the video is short enough.
    if extract.get("has_transcript") or not extract.get("text"):
        return extract

    duration = extract.get("duration") or 0
    if duration and duration <= _WHISPER_FALLBACK_MAX_DURATION_S:
        logger.info(
            f"YouTube {video_id}: no subtitles, trying audio + Whisper "
            f"(duration {duration}s)"
        )
        whisper = _yt_dlp_transcribe(url)
        if whisper.get("text"):
            whisper["source_type"] = "youtube"
            whisper.setdefault("image_urls", []).append(thumb)
            return whisper
        logger.info(f"YouTube {video_id}: Whisper fallback also failed, returning description")
        # Description-only answer: still useful, but mark the limitation.
        extract["reason"] = fetch_errors.WHISPER_FAILED
    elif duration > _WHISPER_FALLBACK_MAX_DURATION_S:
        extract["reason"] = fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
    else:
        extract["reason"] = fetch_errors.NO_TRANSCRIPT

    return extract


def _tweet_id_from_url(url: str) -> str | None:
    match = re.search(r"(?:twitter\.com|x\.com)/\S+/status(?:es)?/(\d+)", url)
    return match.group(1) if match else None


def _fxtwitter_fetch(tweet_id: str) -> dict | None:
    """Fetch tweet data from the fxtwitter community API (free, no auth)."""

    try:
        resp = requests.get(
            f"https://api.fxtwitter.com/status/{tweet_id}",
            timeout=30,
            headers={"User-Agent": "research-companion-bot/1.0"},
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("tweet")
    except Exception as e:
        logger.warning(f"fxtwitter fetch failed for {tweet_id}: {e}")
        return None


def _syndication_fetch(tweet_id: str) -> dict | None:
    """Fetch via X's own syndication API (used for embedded tweets, free, no auth)."""

    try:
        resp = requests.get(
            f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=0",
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if data.get("text") else None
    except Exception as e:
        logger.warning(f"syndication fetch failed for {tweet_id}: {e}")
        return None


def _format_fxtwitter(tweet: dict, url: str) -> dict:
    handle = tweet.get("author", {}).get("screen_name", "")
    author = tweet.get("author", {}).get("name", "")

    image_urls = [
        p["url"] for p in (tweet.get("media") or {}).get("photos", []) if p.get("url")
    ]

    # X Article (long-form "Notes")
    article = tweet.get("article")
    if article:
        title = article.get("title", "")
        body = article.get("text") or tweet.get("text", "")
        text = f"X Article by @{handle}: {title}\n\n{body}"
        return {"text": text[:MAX_CONTENT_CHARS], "title": title or f"Article by @{handle}", "source_type": "social", "image_urls": image_urls}

    text = f"@{handle} ({author}):\n\n{tweet.get('text', '')}"
    return {"text": text[:MAX_CONTENT_CHARS], "title": f"Post by @{handle}", "source_type": "social", "image_urls": image_urls}


def _format_syndication(data: dict, url: str) -> dict:
    user = data.get("user", {})
    handle = user.get("screen_name", "")
    author = user.get("name", "")
    image_urls = [
        m["media_url_https"] for m in (data.get("mediaDetails") or [])
        if m.get("type") == "photo" and m.get("media_url_https")
    ]
    text = f"@{handle} ({author}):\n\n{data.get('text', '')}"
    return {"text": text[:MAX_CONTENT_CHARS], "title": f"Post by @{handle}", "source_type": "social", "image_urls": image_urls}


def _fetch_tweet(url: str) -> dict:
    tweet_id = _tweet_id_from_url(url)

    if tweet_id:
        # 1. fxtwitter (handles X Articles too)
        tweet = _fxtwitter_fetch(tweet_id)
        if tweet:
            return _format_fxtwitter(tweet, url)

        # 2. X syndication API (X's own embed endpoint)
        data = _syndication_fetch(tweet_id)
        if data:
            return _format_syndication(data, url)

    # 3. yt-dlp as last resort
    result = _yt_dlp_extract(url)
    if not (result.get("text") or "").strip():
        result["reason"] = fetch_errors.TWEET_UNAVAILABLE
    return result


def _yt_dlp_extract(url: str) -> dict:
    """Extract metadata + best-effort subtitles from a video URL via yt-dlp.

    Two-pass: metadata first (cheap, hits a different YouTube endpoint and is
    much less rate-limited than subtitle download), then a separate subtitle
    pass whose failures are non-fatal — if YouTube 429s the subtitle download
    we still return title + uploader + description so the analyser has
    *something* to work with.

    Returns: text, title, source_type, has_transcript (bool), duration (s).
    """
    import yt_dlp
    import tempfile, os

    # Pass 1: metadata only. No subtitle/audio downloads — far less likely to 429.
    info = None
    extract_error: str | None = None
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "skip_download": True,
            "writesubtitles": False,
            "ignore_no_formats_error": True,
        }) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        extract_error = str(e)
        logger.warning(f"yt-dlp metadata extract failed for {url}: {e}")

    if not info:
        reason = fetch_errors.FETCH_FAILED
        if extract_error:
            low = extract_error.lower()
            if "429" in low or "rate" in low and "limit" in low:
                reason = fetch_errors.RATE_LIMITED
            elif "private" in low or "unavailable" in low or "removed" in low:
                reason = fetch_errors.VIDEO_UNAVAILABLE
        return {
            "text": "",
            "title": url,
            "source_type": "unknown",
            "has_transcript": False,
            "duration": 0,
            "reason": reason,
        }

    title = info.get("title") or url
    uploader = info.get("uploader") or info.get("channel") or ""
    description = info.get("description") or ""
    duration = int(info.get("duration") or 0)
    source_type = "youtube" if "vimeo" not in url and "youtube" in url else "video"

    # Pass 2: best-effort subtitle download. A 429 here drops us back to the
    # description; it does NOT take the whole extract down with it.
    subtitle_text = ""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            sub_opts = {
                "quiet": True,
                "skip_download": True,
                "ignore_no_formats_error": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en", "en-US", "en-GB"],
                "subtitlesformat": "vtt",
                "paths": {"home": tmpdir},
                "outtmpl": os.path.join("%(id)s", "%(id)s.%(ext)s"),
            }
            with yt_dlp.YoutubeDL(sub_opts) as ydl:
                ydl.download([url])

            for dirpath, _, filenames in os.walk(tmpdir):
                for fname in filenames:
                    if not fname.endswith(".vtt"):
                        continue
                    with open(os.path.join(dirpath, fname), encoding="utf-8", errors="ignore") as f:
                        raw = f.read()
                    lines, seen = [], set()
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit():
                            continue
                        if line not in seen:   # VTT often repeats lines across cue windows
                            seen.add(line)
                            lines.append(line)
                    subtitle_text = " ".join(lines)
                    if subtitle_text:
                        logger.debug(f"Subtitles from {fname}: {len(subtitle_text)} chars")
                        break
                if subtitle_text:
                    break
    except Exception as e:
        logger.info(f"yt-dlp subtitle download failed for {url} (continuing with description): {e}")

    if subtitle_text:
        text = f"{title}\nBy: {uploader}\n\nTranscript:\n{subtitle_text}"
    else:
        text = f"{title}\nBy: {uploader}\n\n{description}".strip()

    return {
        "text": text[:MAX_CONTENT_CHARS],
        "title": title,
        "source_type": source_type,
        "has_transcript": bool(subtitle_text),
        "duration": duration,
    }


def _yt_dlp_transcribe(url: str) -> dict:
    """Download audio from a video URL via yt-dlp and transcribe with Whisper."""
    import yt_dlp
    import tempfile, os

    ydl_opts = {
        "quiet": True,
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "outtmpl": "audio.%(ext)s",
    }
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts["paths"] = {"home": tmpdir}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                raise ValueError("yt-dlp returned no info")

            audio_file = next(
                (os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.endswith(".mp3")),
                None,
            )
            if not audio_file:
                raise ValueError("No audio file produced")

            from bot.transcriber import _transcribe_sync
            transcript = _transcribe_sync(audio_file)

        uploader = info.get("uploader") or info.get("channel") or ""
        title = info.get("title") or url
        text = f"{title}\nBy: {uploader}\n\nTranscript:\n{transcript}".strip()
        return {"text": text[:MAX_CONTENT_CHARS], "title": title, "source_type": "video"}
    except Exception as e:
        logger.warning(f"yt-dlp transcribe failed for {url}: {e}")
        return {"text": "", "title": url, "source_type": "unknown", "reason": fetch_errors.WHISPER_FAILED}


async def _streamyard_fetch(url: str) -> dict:
    """Render the StreamYard watch page, intercept the signed mp4, and transcribe with Whisper."""
    import json as _json, tempfile, os, asyncio as _asyncio

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("playwright not installed — falling back to generic fetch for StreamYard")
        return await _generic_fetch(url)

    # Step 1: intercept the signed vod mp4 URL via headless browser
    video_url = None
    title = url

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            got_video = _asyncio.Event()

            async def on_request(request):
                nonlocal video_url
                if "vods-storage.streamyard.com" in request.url and ".mp4" in request.url:
                    video_url = request.url
                    got_video.set()

            page.on("request", on_request)
            await page.goto(url, wait_until="networkidle", timeout=30_000)

            # Extract title from __NEXT_DATA__
            next_data = await page.evaluate(
                "() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null; }"
            )
            if next_data:
                try:
                    data = _json.loads(next_data)
                    title = data.get("props", {}).get("pageProps", {}).get("metadata", {}).get("title") or url
                except Exception:
                    pass

            # Wait a bit for the video request to fire
            try:
                await _asyncio.wait_for(got_video.wait(), timeout=10)
            except _asyncio.TimeoutError:
                pass

            await browser.close()
    except Exception as e:
        logger.warning(f"Playwright failed for {url}: {e}")
        return {"text": "", "title": url, "source_type": "video", "reason": fetch_errors.STREAMYARD_INTERCEPT_FAILED}

    if not video_url:
        logger.warning(f"StreamYard: no video URL intercepted for {url}")
        return {"text": title, "title": title, "source_type": "video", "reason": fetch_errors.STREAMYARD_INTERCEPT_FAILED}

    logger.info(f"StreamYard: intercepted video URL, downloading and transcribing")

    # Step 2: download the mp4 to a temp file and transcribe
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            tmp_path = f.name
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", video_url) as resp:
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)

        from bot.storage import save_file_from_path
        stored_file_path = save_file_from_path(tmp_path, ".mp4")

        from bot.transcriber import _transcribe_sync
        loop = asyncio.get_running_loop()
        transcript = await loop.run_in_executor(None, _transcribe_sync, tmp_path)
        text = f"{title}\n\nTranscript:\n{transcript}".strip()
        return {"text": text[:MAX_CONTENT_CHARS], "title": title, "source_type": "video", "file_path": stored_file_path}
    except Exception as e:
        logger.warning(f"StreamYard transcription failed for {url}: {e}")
        return {"text": title, "title": title, "source_type": "video", "reason": fetch_errors.WHISPER_FAILED}
    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def _pdf_fetch(url: str) -> dict:
    """Download a PDF and extract text with pdfplumber."""
    import io
    import pdfplumber

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            )
            resp.raise_for_status()
            pdf_bytes = resp.content
    except Exception as e:
        logger.warning(f"PDF download failed for {url}: {e}")
        return {"text": "", "title": url, "source_type": "unknown", "reason": fetch_errors.PDF_DOWNLOAD_FAILED}

    try:
        pages_text = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            title = (pdf.metadata or {}).get("Title") or url
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(page_text)
        text = "\n\n".join(pages_text)
    except Exception as e:
        logger.warning(f"pdfplumber extraction failed for {url}: {e}")
        return {"text": "", "title": url, "source_type": "unknown", "reason": fetch_errors.NO_TEXT_EXTRACTED}

    if not text.strip():
        logger.warning(f"PDF at {url} yielded no text — likely image-based; OCR not available")
        return {"text": "", "title": title, "source_type": "pdf", "reason": fetch_errors.IMAGE_ONLY_PDF}

    return {"text": text[:MAX_CONTENT_CHARS], "title": title, "source_type": "pdf"}


def _curl_cffi_get(url: str):
    """Sync GET with Chrome TLS/HTTP2 impersonation. Clears Cloudflare managed
    challenges that plain httpx trips on (e.g. blog.cathy-moore.com), and is a
    drop-in superset for vanilla HTML article fetches."""
    from curl_cffi import requests as cr
    return cr.get(url, impersonate="chrome124", timeout=15, allow_redirects=True)


# A real article is essentially never this short. Anything below this from a
# generic HTML fetch is a wall/placeholder, not content.
_MIN_ARTICLE_CHARS = 200
# Signature phrases only count as a wall when the whole extraction is short —
# a long article that merely *mentions* "subscribe to continue" isn't a wall.
_WALL_SIGNATURE_MAX_CHARS = 1000

_JS_WALL_SIGNATURES = (
    "enable js",
    "enable javascript",
    "javascript is disabled",
    "javascript is required",
    "please enable cookies",
    "disable any ad blocker",
    "turn off your ad blocker",
)
_PAYWALL_SIGNATURES = (
    "subscribe to continue",
    "subscribe to read",
    "sign in to read",
    "sign in to continue",
    "create a free account",
    "create an account to continue",
    "this article is for subscribers",
    "this post is for paid subscribers",
    "become a paid subscriber",
    "you've reached your",
    "you have reached your",
)


def _wall_reason(text: str | None) -> str | None:
    """Detect JS/ad-block walls and paywall teasers in extracted article text.

    Walls return short boilerplate ("Please enable JS", "Subscribe to continue")
    that would otherwise be analysed as if it were the article. Returns a
    fetch_errors reason, or None when the text looks like real content.
    """
    clean = (text or "").strip()
    low = clean.lower()
    if len(clean) < _WALL_SIGNATURE_MAX_CHARS:
        if any(s in low for s in _JS_WALL_SIGNATURES):
            return fetch_errors.JS_REQUIRED
        if any(s in low for s in _PAYWALL_SIGNATURES):
            return fetch_errors.PAYWALLED
    if len(clean) < _MIN_ARTICLE_CHARS:
        return fetch_errors.NO_TEXT_EXTRACTED
    return None


async def _generic_fetch(url: str) -> dict:
    try:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, _curl_cffi_get, url)
        html = resp.text
    except Exception as e:
        logger.warning(f"Generic fetch failed for {url}: {e}")
        return {"text": "", "title": url, "source_type": "unknown", "reason": fetch_errors.FETCH_FAILED}

    logger.debug(f"Fetched {url} — status={resp.status_code} len={len(html)}")

    import trafilatura  # heavy import — defer to first generic fetch

    # 1. trafilatura strict
    text = trafilatura.extract(html, include_comments=False, include_tables=False)

    # 2. trafilatura with recall mode (less strict)
    if not text:
        text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True)
        if text:
            logger.debug(f"trafilatura favor_recall extracted {len(text)} chars from {url}")

    if not text:
        logger.debug(f"trafilatura failed for {url}, trying BeautifulSoup")

    # 3. BeautifulSoup fallback — extract visible text from article/main/body
    if not text:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            container = soup.find("article") or soup.find("main") or soup.find("body")
            if container:
                text = container.get_text(separator="\n", strip=True)
        except Exception as e:
            logger.warning(f"BeautifulSoup fallback failed for {url}: {e}")

    title = url
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
    except Exception:
        pass

    reason = _wall_reason(text)
    if reason:
        if reason in (fetch_errors.JS_REQUIRED, fetch_errors.PAYWALLED):
            logger.info(f"Wall detected ({reason}) for {url}: {(text or '').strip()[:80]!r}")
        return {"text": "", "title": title, "source_type": "article", "reason": reason}

    return {"text": text[:MAX_CONTENT_CHARS], "title": title, "source_type": "article"}


def _domain_matches(domain: str, *targets: str) -> bool:
    """Check if domain equals or is a subdomain of any target."""
    return any(domain == t or domain.endswith(f".{t}") for t in targets)


async def fetch_url(url: str) -> dict:
    """Public entry point. Wraps `_fetch_url_uncached` with a per-URL cache so
    repeat submissions, retries, and concurrent fetches of the same URL don't
    each hit upstream. Failed fetches (empty `text`) are NOT cached so the
    next attempt is fresh."""
    from bot.db import get_cached_fetch, set_cached_fetch

    cached = get_cached_fetch(url)
    if cached is not None:
        logger.info(f"url_cache hit for {url}")
        return cached

    result = await _fetch_url_uncached(url)

    if (result.get("text") or "").strip():
        try:
            set_cached_fetch(url, result)
        except Exception as e:
            logger.warning(f"url_cache write failed for {url}: {e}")

    return result


async def _fetch_url_uncached(url: str) -> dict:
    """The actual routing logic — keep this as the only place that knows the
    source-specific fetchers. `fetch_url` is the cache layer in front."""
    domain = urlparse(url).netloc.lower()

    if _domain_matches(domain, "youtube.com", "youtu.be"):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _youtube_transcript, url)

    if _domain_matches(domain, "streamyard.com"):
        return await _streamyard_fetch(url)

    if _domain_matches(domain, "vimeo.com"):
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _yt_dlp_extract, url)
        if result["text"].strip():
            return result
        logger.info(f"No subtitles for {url}, falling back to Whisper transcription")
        return await loop.run_in_executor(None, _yt_dlp_transcribe, url)

    if _domain_matches(domain, "twitter.com", "x.com", "t.co"):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _fetch_tweet, url)

    if urlparse(url).path.lower().endswith(".pdf"):
        return await _pdf_fetch(url)

    return await _generic_fetch(url)
