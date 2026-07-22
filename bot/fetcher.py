import asyncio
import html
import logging
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
import requests

from bot import fetch_errors
from bot.config import CONTACT_EMAIL, MAX_CONTENT_CHARS
from bot.ssrf import BlockedURLError, assert_public_url

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


# Per-tier ceilings on how long a captionless video may be before we attempt
# audio download + transcription (Groq, see bot.transcriber). Hosted
# transcription is fast enough that the real cost is the audio download, so
# these are policy/cost levers rather than compute limits. Every web request
# runs through the async job path (`/api/job`), which has no Worker-side
# analysis timeout, so both tiers use their full cap everywhere.
WHISPER_MAX_DURATION_ANON_S = 30 * 60        # 30 min — anonymous web tries
# 2.5 h — signed-in users. X's own long-video uploads run up to ~2 h (a real
# example ran 2h00m18s), so a flat 2 h cap clipped posts right at that limit;
# this adds headroom above the platform's own ceiling rather than matching it
# exactly. Same constant for every signed-in user — no per-account override.
WHISPER_MAX_DURATION_SIGNED_IN_S = int(2.5 * 60 * 60)


def whisper_cap_for(*, signed_in: bool) -> int:
    """Default per-tier transcription duration ceiling (seconds)."""
    return WHISPER_MAX_DURATION_SIGNED_IN_S if signed_in else WHISPER_MAX_DURATION_ANON_S


def _lang_base(code: str) -> str:
    """Bare language subtag, e.g. 'en-US' / 'en-GB' → 'en', 'zh-Hans' → 'zh'."""
    return (code or "").split("-")[0].lower()


def _select_transcript(transcripts: list):
    """Choose which caption track to summarise from.

    Creators often upload many *manual translation* tracks (this video has 20+:
    Arabic, Bulgarian, Chinese, …). Naively taking the first manual track picks
    whichever language YouTube lists first — alphabetically Arabic — so an
    English video gets summarised in Arabic (issue #57).

    The auto-generated (ASR) track is transcribed from the actual audio, so its
    language is the video's spoken language. Anchor on it: prefer the manual
    track in that language (human-authored, cleaner), then the ASR track
    itself. Only when there's no ASR track to anchor on do we fall back to a
    manual track — English first (the common original), else whatever's listed
    first — and finally any generated track.
    """
    generated = next((t for t in transcripts if t.is_generated), None)
    manuals = [t for t in transcripts if not t.is_generated]

    original_lang = generated.language_code if generated else None
    if original_lang:
        manual_in_original = next(
            (t for t in manuals if _lang_base(t.language_code) == _lang_base(original_lang)),
            None,
        )
        return manual_in_original or generated

    english_manual = next((t for t in manuals if _lang_base(t.language_code) == "en"), None)
    return english_manual or (manuals[0] if manuals else None) or generated


def _youtube_transcript(url: str, max_whisper_duration: int = WHISPER_MAX_DURATION_ANON_S) -> dict:
    from youtube_transcript_api import YouTubeTranscriptApi

    match = _YT_PATTERNS.search(url)
    if not match:
        return _yt_dlp_extract(url)

    video_id = match.group(1)
    thumb = _youtube_thumbnail(video_id)
    try:
        api = YouTubeTranscriptApi()
        # Enumerate every available transcript and pick the one in the video's
        # spoken language (see _select_transcript) — never translate, so the LLM
        # summarises in the speaker's tongue. We don't force 'en' (that silently
        # dropped non-English-captioned videos to yt-dlp) nor take the first
        # manual track (that summarised multi-subtitle videos in a random
        # language — issue #57).
        transcripts = list(api.list(video_id))
        transcript = _select_transcript(transcripts)
        if transcript is not None:
            fetched = transcript.fetch()
            text = " ".join(snippet.text for snippet in fetched)
            title = _youtube_oembed_title(url) or f"YouTube video ({video_id})"
            return {
                "text": f"{title}\n\nTranscript:\n{text}"[:MAX_CONTENT_CHARS],
                "title": title,
                "source_type": "youtube",
                "image_urls": [thumb],
                "language": transcript.language_code,
                "transcript_source": "youtube",
            }
    except Exception:
        logger.info(f"No transcript for {video_id}, falling back to yt-dlp")

    extract = _yt_dlp_extract(url)
    extract.setdefault("image_urls", []).append(thumb)

    # If yt-dlp also got us a transcript (or extraction died entirely), use what
    # we have. The Whisper branch only kicks in when we're left with a
    # description-only answer AND the video is short enough.
    if extract.get("has_transcript"):
        extract.setdefault("transcript_source", "youtube")
        return extract
    if not extract.get("text"):
        # Genuine extraction failure (no description either) — pipeline will
        # surface ERR_NO_TRANSCRIPT. Tag for the audit log.
        extract.setdefault("transcript_source", "none")
        return extract

    duration = extract.get("duration") or 0
    if duration and duration <= max_whisper_duration:
        logger.info(
            f"YouTube {video_id}: no subtitles, trying audio + Whisper "
            f"(duration {duration}s, cap {max_whisper_duration}s)"
        )
        whisper = _yt_dlp_transcribe(url)
        if whisper.get("text"):
            whisper["source_type"] = "youtube"
            whisper.setdefault("image_urls", []).append(thumb)
            whisper["transcript_source"] = "whisper"
            return whisper
        logger.info(f"YouTube {video_id}: Whisper fallback also failed, returning description")
        # Description-only answer: still useful, but mark the limitation.
        extract["reason"] = fetch_errors.WHISPER_FAILED
        extract["transcript_source"] = "description"
    elif duration > max_whisper_duration:
        extract["reason"] = fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
        extract["transcript_source"] = "description"
    else:
        extract["reason"] = fetch_errors.NO_TRANSCRIPT
        extract["transcript_source"] = "description"

    return extract


_VIMEO_HASH_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)


def _vimeo_id_and_hash(url: str) -> tuple[str, str | None] | None:
    """Pull the numeric video id (and, for unlisted videos, the trailing hash)
    out of any Vimeo URL shape: vimeo.com/123, vimeo.com/123/abcdef0123,
    vimeo.com/channels/x/123, player.vimeo.com/video/123, etc."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    for i, seg in enumerate(segments):
        if seg.isdigit() and len(seg) >= 6:
            video_hash = None
            if i + 1 < len(segments) and _VIMEO_HASH_RE.match(segments[i + 1]):
                video_hash = segments[i + 1]
            return seg, video_hash
    return None


def _vimeo_config(video_id: str, video_hash: str | None) -> dict | None:
    """Fetch Vimeo's own player config for a video (public, unauthenticated —
    the same JSON the embedded player itself loads)."""
    try:
        resp = requests.get(
            f"https://player.vimeo.com/video/{video_id}/config",
            params={"h": video_hash} if video_hash else {},
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://vimeo.com/",
            },
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        logger.info(f"Vimeo config fetch failed for {video_id}: {e}")
        return None


def _select_vimeo_track(tracks: list[dict]) -> dict | None:
    english = next((t for t in tracks if (t.get("lang") or "").lower().startswith("en")), None)
    return english or (tracks[0] if tracks else None)


def _vimeo_native_transcript(config: dict, video_id: str) -> dict | None:
    """Try Vimeo's own caption track (same JSON the embedded player loads) —
    a cheap HTTP fetch that works regardless of video length, unlike the
    yt-dlp + Whisper fallback which is bounded by max_whisper_duration. This is
    what makes long-form Vimeo videos with captions transcribable at all (#101)."""
    video_meta = config.get("video") or {}
    tracks = ((config.get("request") or {}).get("text_tracks")) or []
    track = _select_vimeo_track(tracks)
    if not track or not track.get("url"):
        return None

    try:
        vtt_url = urljoin("https://player.vimeo.com/", track["url"])
        resp = requests.get(vtt_url, timeout=15)
        resp.raise_for_status()
        text = _vtt_to_text(resp.text)
    except Exception as e:
        logger.info(f"Vimeo caption download failed for {video_id}: {e}")
        return None

    if not text:
        return None

    title = video_meta.get("title") or f"Vimeo video ({video_id})"
    thumbs = video_meta.get("thumbs") or {}
    image_urls = [thumbs[max(thumbs, key=lambda k: int(k) if k.isdigit() else 0)]] if thumbs else []
    return {
        "text": f"{title}\n\nTranscript:\n{text}"[:MAX_CONTENT_CHARS],
        "title": title,
        "source_type": "video",
        "image_urls": image_urls,
        "language": track.get("lang"),
        "duration": int(video_meta.get("duration") or 0),
        "transcript_source": "vimeo",
    }


def _vimeo_transcript(url: str, max_whisper_duration: int = WHISPER_MAX_DURATION_ANON_S) -> dict:
    """Vimeo counterpart to `_youtube_transcript`: try Vimeo's own caption
    track first (cheap, no duration limit), then fall back to yt-dlp
    metadata/subtitles, then Whisper for captionless videos short enough to
    fit the caller's tier."""
    ids = _vimeo_id_and_hash(url)
    if ids:
        video_id, video_hash = ids
        config = _vimeo_config(video_id, video_hash)
        if config:
            native = _vimeo_native_transcript(config, video_id)
            if native:
                return native
        logger.info(f"No native captions for Vimeo {video_id}, falling back to yt-dlp")

    extract = _yt_dlp_extract(url)
    if extract.get("has_transcript"):
        extract.setdefault("transcript_source", "vimeo")
        return extract
    if not extract.get("text"):
        extract.setdefault("transcript_source", "none")
        return extract

    duration = extract.get("duration") or 0
    if duration and duration <= max_whisper_duration:
        logger.info(
            f"Vimeo {url}: no subtitles, trying audio + Whisper "
            f"(duration {duration}s, cap {max_whisper_duration}s)"
        )
        whisper = _yt_dlp_transcribe(url)
        if whisper.get("text"):
            whisper["source_type"] = "video"
            whisper["transcript_source"] = "whisper"
            return whisper
        logger.info(f"Vimeo {url}: Whisper fallback also failed, returning description")
        extract["reason"] = fetch_errors.WHISPER_FAILED
        extract["transcript_source"] = "description"
    elif duration > max_whisper_duration:
        extract["reason"] = fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
        extract["transcript_source"] = "description"
    else:
        extract["reason"] = fetch_errors.NO_TRANSCRIPT
        extract["transcript_source"] = "description"

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


def _article_body_from_blocks(article: dict) -> str:
    """fxtwitter puts X Article bodies in a Draft.js-style block list, not a
    flat `text` field — reconstruct it by joining each block's text."""
    blocks = (article.get("content") or {}).get("blocks") or []
    return "\n\n".join(b["text"] for b in blocks if b.get("text"))


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
        body = (
            _article_body_from_blocks(article)
            or article.get("preview_text")
            or tweet.get("text", "")
        )
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


def _tweet_video_transcript(
    result: dict, tweet: dict, url: str, max_whisper_duration: int
) -> dict:
    """If the tweet has an embedded video, transcribe it with Whisper
    (duration-capped, mirroring `_youtube_transcript`/`_vimeo_transcript`) and
    append the transcript to the post text. Without this, a tweet whose real
    content is a long embedded video summarises off the caption alone — often
    just a couple hundred words even when the video runs for hours."""
    videos = (tweet.get("media") or {}).get("videos") or []
    if not videos:
        return result

    duration = int(videos[0].get("duration") or 0)
    if duration > max_whisper_duration:
        result["reason"] = fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
        result["transcript_source"] = "description"
        return result
    if not duration:
        return result

    whisper = _yt_dlp_transcribe(url)
    transcript = whisper.get("transcript") or whisper.get("text")
    if transcript:
        result["text"] = f"{result['text']}\n\nVideo transcript:\n{transcript}"[:MAX_CONTENT_CHARS]
        result["transcript_source"] = "whisper"
        result["duration"] = duration
    else:
        result["reason"] = fetch_errors.WHISPER_FAILED
        result["transcript_source"] = "description"
    return result


def _fetch_tweet(url: str, max_whisper_duration: int = WHISPER_MAX_DURATION_ANON_S) -> dict:
    tweet_id = _tweet_id_from_url(url)

    if tweet_id:
        # 1. fxtwitter (handles X Articles too, and is the only tier that
        # surfaces embedded video — see _tweet_video_transcript)
        tweet = _fxtwitter_fetch(tweet_id)
        if tweet:
            result = _format_fxtwitter(tweet, url)
            return _tweet_video_transcript(result, tweet, url, max_whisper_duration)

        # 2. X syndication API (X's own embed endpoint) — photos only, no
        # video field, so a video tweet falling back here loses transcription.
        data = _syndication_fetch(tweet_id)
        if data:
            return _format_syndication(data, url)

    # 3. yt-dlp as last resort (weird URL shapes, or both APIs down)
    extract = _yt_dlp_extract(url)
    if extract.get("has_transcript"):
        extract.setdefault("transcript_source", "twitter")
        return extract
    if not extract.get("text"):
        extract["reason"] = fetch_errors.TWEET_UNAVAILABLE
        return extract

    duration = extract.get("duration") or 0
    if duration and duration <= max_whisper_duration:
        whisper = _yt_dlp_transcribe(url)
        if whisper.get("text"):
            whisper["source_type"] = "social"
            whisper["transcript_source"] = "whisper"
            return whisper
        extract["reason"] = fetch_errors.WHISPER_FAILED
        extract["transcript_source"] = "description"
    elif duration > max_whisper_duration:
        extract["reason"] = fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
        extract["transcript_source"] = "description"
    return extract


def _vtt_to_text(raw: str) -> str:
    """Flatten a WebVTT file to plain text, deduping the repeated lines VTT
    cue windows commonly carry."""
    lines, seen = [], set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit():
            continue
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines)


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
    language = info.get("language") or ""
    # yt-dlp returns YYYYMMDD as a string; normalise to ISO so it flows
    # cleanly into prompts and (later) into stored item rows.
    upload_date_raw = info.get("upload_date") or ""
    published_at = (
        f"{upload_date_raw[:4]}-{upload_date_raw[4:6]}-{upload_date_raw[6:8]}"
        if len(upload_date_raw) == 8 and upload_date_raw.isdigit()
        else ""
    )
    source_type = "youtube" if "vimeo" not in url and "youtube" in url else "video"

    # Pass 2: best-effort subtitle download. Prefer the video's detected
    # language so non-English videos don't degrade to description-only, then
    # fall through to common English variants for back-compat. A 429 here
    # drops us back to the description; it does NOT take the whole extract
    # down with it.
    lang_pref: list[str] = []
    for lang in (language, "en", "en-US", "en-GB"):
        if lang and lang not in lang_pref:
            lang_pref.append(lang)

    subtitle_text = ""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            sub_opts = {
                "quiet": True,
                "skip_download": True,
                "ignore_no_formats_error": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": lang_pref,
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
                    subtitle_text = _vtt_to_text(raw)
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
        "language": language,
        "published_at": published_at,
    }


def _yt_dlp_transcribe(url: str) -> dict:
    """Download audio from a video URL via yt-dlp and transcribe with Whisper."""
    import yt_dlp
    import tempfile, os

    ydl_opts = {
        "quiet": True,
        # Prefer a low-bitrate audio stream (YouTube's ~50 kbps Opus) over the
        # 160 kbps default: Whisper only needs 16 kHz speech, so the high
        # bitrate buys nothing and a 2 h best-quality download is 100 MB+ of
        # transient disk on the 1 GB box. We also drop the FFmpegExtractAudio
        # re-encode — the transcriber does a single transcode to the compact
        # upload format, so a second high-quality intermediate is pure waste.
        "format": "ba[abr<=64]/ba/b",
        "outtmpl": "audio.%(ext)s",
        # Safety net for direct-audio URLs whose duration can't be probed: cap
        # the transient download so a runaway file can't fill the 1 GB box.
        # ~64 kbps × this ≈ 7 h of audio, well past any real episode.
        "max_filesize": 200 * 1024 * 1024,
    }
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts["paths"] = {"home": tmpdir}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                raise ValueError("yt-dlp returned no info")

            # We didn't re-encode, so the file keeps its native extension
            # (.webm/.m4a/.opus). Grab whatever single media file landed.
            audio_file = next(
                (os.path.join(tmpdir, f) for f in sorted(os.listdir(tmpdir)) if f.startswith("audio.")),
                None,
            )
            if not audio_file:
                raise ValueError("No audio file produced")

            from bot.transcriber import _transcribe_sync
            transcript = _transcribe_sync(audio_file)

        uploader = info.get("uploader") or info.get("channel") or ""
        title = info.get("title") or url
        text = f"{title}\nBy: {uploader}\n\nTranscript:\n{transcript}".strip()
        return {
            "text": text[:MAX_CONTENT_CHARS],
            "title": title,
            "source_type": "video",
            "transcript": transcript,
        }
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

        # The mp4 is downloaded to a temp file, transcribed, and deleted in the
        # `finally` below — we do NOT persist third-party media to disk.
        from bot.transcriber import _transcribe_sync
        loop = asyncio.get_running_loop()
        transcript = await loop.run_in_executor(None, _transcribe_sync, tmp_path)
        text = f"{title}\n\nTranscript:\n{transcript}".strip()
        return {"text": text[:MAX_CONTENT_CHARS], "title": title, "source_type": "video"}
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
# Block / removed / not-found boilerplate that extracts longer than the JS-wall
# heuristics but is still not real content — most often Reddit pages served to
# datacenter IPs, which would otherwise be analysed into a meaningless verdict
# (#67). Matched anywhere in the (short) extracted text.
_NO_CONTENT_SIGNATURES = (
    "you've been blocked by network security",
    "the page you requested does not exist",
    "this post has been removed",
    "sorry, this post has been removed",
    "this community has been banned",
    "this account has been suspended",
    "page not found",
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
        if any(s in low for s in _NO_CONTENT_SIGNATURES):
            return fetch_errors.NO_TEXT_EXTRACTED
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


# Reddit's modern www/app pages are a client-rendered JS shell — a plain fetch
# extracts nothing, so a Reddit link used to either error or (worse) get a
# meaningless verdict off page chrome (#67). old.reddit.com still serves the
# real server-rendered post, so we rewrite the host. (Media subdomains
# i.redd.it / v.redd.it are left alone — they aren't articles.) Reddit also
# Cloudflare-gates datacenter IPs, so this is best-effort; when it's blocked
# the fetch surfaces a clean Reddit-specific error rather than a bogus verdict.
def _to_old_reddit(url: str) -> str:
    parts = urlparse(url)
    host = parts.hostname or ""
    if host in ("www.reddit.com", "reddit.com", "np.reddit.com", "m.reddit.com"):
        return parts._replace(netloc="old.reddit.com").geturl()
    return url


# Direct audio (podcast) URLs end up here instead of the HTML article path:
# running trafilatura on audio bytes yields an empty tree and no text (#8).
# Detection is by file suffix on the URL path — precise and zero-cost. The
# anchor.fm/podbean style "play" links carry the real .mp3 at the end of the
# path, so the suffix check catches them too.
_AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4b")


def _looks_like_audio(url: str) -> bool:
    try:
        return urlparse(url).path.lower().endswith(_AUDIO_SUFFIXES)
    except ValueError:
        return False


def _transcribe_audio_url(url: str, max_whisper_duration: int) -> dict:
    """Fetch + transcribe a direct audio URL (#8).

    Probes duration first (cheap metadata pass) so a multi-hour episode doesn't
    silently blow the Whisper budget — mirrors the Vimeo path. Then reuses the
    yt-dlp audio download + Whisper transcription. yt-dlp handles direct media
    URLs natively, including the redirect chains podcast CDNs use.
    """
    import yt_dlp

    title = url
    duration = 0
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        title = info.get("title") or url
        duration = int(info.get("duration") or 0)
    except Exception as e:
        # Many raw audio files carry no probeable metadata — fall through to
        # transcription, which the per-download filesize cap still bounds.
        logger.info("audio duration probe failed for %s: %s", url, e)

    if duration and duration > max_whisper_duration:
        logger.info("Audio %s: %ds exceeds cap %ds", url, duration, max_whisper_duration)
        return {"text": title, "title": title, "source_type": "audio",
                "reason": fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER}

    result = _yt_dlp_transcribe(url)
    result["source_type"] = "audio"
    return result


async def fetch_url(
    url: str,
    *,
    max_whisper_duration: int = WHISPER_MAX_DURATION_ANON_S,
    skip_cache: bool = False,
) -> dict:
    """Public entry point. Wraps `_fetch_url_uncached` with a per-URL cache so
    repeat submissions, retries, and concurrent fetches of the same URL don't
    each hit upstream. Failed fetches (empty `text`) are NOT cached so the
    next attempt is fresh.

    `max_whisper_duration` is the captionless-video transcription ceiling for
    this caller's tier (see `whisper_cap_for`). It only changes *whether* we
    transcribe, never the transcript itself, so the cache stays keyed by URL —
    except the one tier-dependent degraded outcome (`video_too_long_for_whisper`)
    which we never cache, so a stricter tier can't poison a more generous one.

    `skip_cache=True` bypasses the cache read (a stale/bad entry stays stale
    forever otherwise — e.g. a fetcher bug cached a degraded result before the
    fix shipped) but still overwrites the entry with the fresh result, so the
    next normal request benefits too. Used by the admin retrigger endpoint.
    """
    from bot.db import get_cached_fetch, set_cached_fetch

    cached = None if skip_cache else get_cached_fetch(url)
    if cached is not None:
        logger.info(f"url_cache hit for {url}")
        return cached

    result = await _fetch_url_uncached(url, max_whisper_duration=max_whisper_duration)

    cacheable = (
        (result.get("text") or "").strip()
        and result.get("reason") != fetch_errors.VIDEO_TOO_LONG_FOR_WHISPER
    )
    if cacheable:
        try:
            set_cached_fetch(url, result)
        except Exception as e:
            logger.warning(f"url_cache write failed for {url}: {e}")

    return result


# --- Academic / scholarly fallback ------------------------------------------
# Many publisher article pages (Springer, Wiley, Elsevier, …) JS- or login-wall
# the HTML, so a plain fetch gets a "please enable JavaScript" stub. But the
# same articles are addressable by DOI through free, no-key scholarly APIs that
# return a clean abstract (Crossref) or an open-access copy (Unpaywall). That's
# a single on-behalf-of-user lookup keyed by an identifier in the URL — not
# crawling — and it turns a dead-end wall into something analysable.

# A DOI is "10." + a 4–9 digit registrant + "/" + a liberal suffix. In a URL the
# suffix runs until a query/fragment; we then trim path tails that clearly
# aren't part of the DOI (e.g. /fulltext, .pdf) that publishers append.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>?#]+", re.IGNORECASE)
_DOI_TRAILING = re.compile(
    r"(?:/(?:full|fulltext|abstract|pdf|epdf|meta|references|citations))+$|"
    r"\.(?:pdf|html?|full|abstract)$",
    re.IGNORECASE,
)


def _extract_doi(url: str) -> str | None:
    """Pull a DOI out of an article URL, or None. Path-only (ignores query)."""
    m = _DOI_RE.search(unquote(urlparse(url).path))
    if not m:
        return None
    doi = m.group(0).rstrip(").,;'\"")
    doi = _DOI_TRAILING.sub("", doi)
    return doi or None


def _strip_jats(s: str) -> str:
    """Crossref abstracts come as JATS/XML (<jats:p>…</jats:p>). Reduce to plain
    text and drop a redundant leading 'Abstract' heading."""
    if not s:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", s))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^abstract[:\s]+", "", text, flags=re.IGNORECASE).strip()


def _format_authors(authors) -> str:
    if not isinstance(authors, list):
        return ""
    names = []
    for a in authors[:8]:
        if isinstance(a, dict):
            name = " ".join(p for p in (a.get("given"), a.get("family")) if p).strip()
            if name:
                names.append(name)
    return ", ".join(names)


def _crossref_fetch(doi: str) -> dict | None:
    """Title + abstract + authors for a DOI from Crossref (free, no key)."""
    try:
        resp = requests.get(
            f"https://api.crossref.org/works/{quote(doi, safe='/')}",
            headers={"User-Agent": f"filter-fyi/1.0 (mailto:{CONTACT_EMAIL})"},
            timeout=10,
        )
    except Exception as e:
        logger.info("Crossref fetch failed for %s: %s", doi, e)
        return None
    if resp.status_code != 200:
        return None
    try:
        msg = resp.json().get("message", {})
    except Exception:
        return None
    title = " ".join(t for t in (msg.get("title") or []) if t).strip() or None
    return {
        "title": title,
        "abstract": _strip_jats(msg.get("abstract", "")) or None,
        "authors": _format_authors(msg.get("author")),
    }


def _unpaywall_oa_url(doi: str) -> str | None:
    """Open-access full-text URL for a DOI from Unpaywall, or None."""
    try:
        resp = requests.get(
            f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}",
            params={"email": CONTACT_EMAIL},
            timeout=10,
        )
    except Exception as e:
        logger.info("Unpaywall fetch failed for %s: %s", doi, e)
        return None
    if resp.status_code != 200:
        return None
    try:
        loc = (resp.json() or {}).get("best_oa_location") or {}
    except Exception:
        return None
    return loc.get("url_for_pdf") or loc.get("url") or None


async def _academic_fetch(url: str, doi: str) -> dict | None:
    """Best-effort content for a walled academic article via scholarly APIs.
    Prefers Unpaywall open-access full text, falls back to the Crossref
    abstract. Returns None when neither yields text (caller keeps the wall)."""
    loop = asyncio.get_running_loop()

    # 1. Open-access full text (PubMed Central, repositories, publisher OA).
    oa_url = await loop.run_in_executor(None, _unpaywall_oa_url, doi)
    if oa_url:
        try:
            assert_public_url(oa_url)  # OA URL is attacker-influenceable via the API
            is_pdf = oa_url.split("?")[0].lower().endswith(".pdf")
            oa = await (_pdf_fetch(oa_url) if is_pdf else _generic_fetch(oa_url))
            if oa.get("text", "").strip():
                oa["source_type"] = "academic"
                logger.info("Academic OA full text recovered for DOI %s via %s", doi, oa_url)
                return oa
        except BlockedURLError as e:
            logger.info("Unpaywall OA URL blocked for %s: %s", doi, e)
        except Exception as e:
            logger.info("Unpaywall OA fetch failed for %s (%s): %s", doi, oa_url, e)

    # 2. Crossref abstract — thin, but real content and enough for a verdict.
    meta = await loop.run_in_executor(None, _crossref_fetch, doi)
    if meta and meta.get("abstract"):
        title = meta.get("title") or url
        header = title if not meta.get("authors") else f"{title}\nBy: {meta['authors']}"
        logger.info("Academic abstract recovered for DOI %s via Crossref", doi)
        return {
            "text": f"{header}\n\nAbstract:\n{meta['abstract']}"[:MAX_CONTENT_CHARS],
            "title": title,
            "source_type": "academic",
        }
    return None


async def _fetch_url_uncached(
    url: str, *, max_whisper_duration: int = WHISPER_MAX_DURATION_ANON_S
) -> dict:
    """The actual routing logic — keep this as the only place that knows the
    source-specific fetchers. `fetch_url` is the cache layer in front."""
    # SSRF guard: refuse anything that resolves to a non-public address before
    # any network client touches it. Empty text + reason flows through the
    # normal "couldn't extract" path so the user gets a clean message.
    try:
        assert_public_url(url)
    except BlockedURLError as e:
        logger.warning("Blocked non-public URL %s: %s", url, e)
        return {"text": "", "title": url, "source_type": "unknown", "reason": fetch_errors.BLOCKED_URL}

    domain = urlparse(url).netloc.lower()

    if _domain_matches(domain, "youtube.com", "youtu.be"):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _youtube_transcript, url, max_whisper_duration
        )

    if _domain_matches(domain, "streamyard.com"):
        return await _streamyard_fetch(url)

    if _domain_matches(domain, "vimeo.com"):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _vimeo_transcript, url, max_whisper_duration)

    if _domain_matches(domain, "twitter.com", "x.com", "t.co"):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _fetch_tweet, url, max_whisper_duration)

    if _looks_like_audio(url):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _transcribe_audio_url, url, max_whisper_duration
        )

    if _domain_matches(domain, "reddit.com"):
        # Server-rendered old.reddit instead of the www JS shell; same generic
        # extraction + wall/no-content detection handles the rest.
        return await _generic_fetch(_to_old_reddit(url))

    if urlparse(url).path.lower().endswith(".pdf"):
        return await _pdf_fetch(url)

    # Generic article/blog fetch. We deliberately do NOT consult robots.txt
    # here: every fetch is a single, user-initiated request for a page the
    # person already intends to read — a user agent acting on their behalf,
    # not a crawler indexing the site. (Same distinction Google draws for its
    # user-triggered fetchers.) SSRF guard, rate limits and summary-only
    # retention still apply.
    result = await _generic_fetch(url)

    # If the page walled us (Springer/Wiley/… JS- or login-gate) and the URL
    # carries a DOI, try open scholarly APIs for an abstract or open-access
    # copy before surfacing the wall. Only on failure modes — a good extract
    # is left untouched so non-walled sites keep their full text.
    if result.get("reason") in (
        fetch_errors.JS_REQUIRED,
        fetch_errors.PAYWALLED,
        fetch_errors.NO_TEXT_EXTRACTED,
    ):
        doi = _extract_doi(url)
        if doi:
            logger.info("Article walled (%s); trying scholarly APIs for DOI %s", result["reason"], doi)
            academic = await _academic_fetch(url, doi)
            if academic:
                return academic
    return result
