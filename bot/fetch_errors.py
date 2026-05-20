"""Reason codes attached to a fetch result when extraction is empty or partial.

The fetcher records *why* a URL couldn't be turned into usable text; handlers
and the Cloudflare Worker render a friendly explanation instead of a generic
"could not extract content" line.

Keep this set small and stable — the log scanner also looks at it to decide
whether a warning is an already-handled user limitation or a real bug.
"""

# Empty-text reasons
NO_TRANSCRIPT = "no_transcript"
VIDEO_TOO_LONG_FOR_WHISPER = "video_too_long_for_whisper"
WHISPER_FAILED = "whisper_failed"
STREAMYARD_INTERCEPT_FAILED = "streamyard_intercept_failed"
IMAGE_ONLY_PDF = "image_only_pdf"
PDF_DOWNLOAD_FAILED = "pdf_download_failed"
TWEET_UNAVAILABLE = "tweet_unavailable"
RATE_LIMITED = "rate_limited"
FETCH_FAILED = "fetch_failed"
NO_TEXT_EXTRACTED = "no_text_extracted"
VIDEO_UNAVAILABLE = "video_unavailable"


_USER_MESSAGES: dict[str, str] = {
    NO_TRANSCRIPT: (
        "This video has no subtitles or transcript available, so there's nothing "
        "to analyse. Try sharing a video with captions enabled."
    ),
    VIDEO_TOO_LONG_FOR_WHISPER: (
        "This video has no subtitles and is too long for automatic transcription. "
        "Try a shorter video (under 3 minutes) or one with captions."
    ),
    WHISPER_FAILED: (
        "Couldn't transcribe this video's audio. The source may be blocking "
        "downloads or the audio track is unusable."
    ),
    STREAMYARD_INTERCEPT_FAILED: (
        "Couldn't capture the StreamYard recording. The page may require sign-in "
        "or the video isn't published yet."
    ),
    IMAGE_ONLY_PDF: (
        "This PDF is scanned/image-based — no selectable text. OCR isn't enabled "
        "yet, so there's nothing to analyse."
    ),
    PDF_DOWNLOAD_FAILED: (
        "Couldn't download the PDF. The link may be behind a login wall or "
        "temporarily unavailable."
    ),
    TWEET_UNAVAILABLE: (
        "This post is unavailable — it may be deleted, from a private account, "
        "or restricted in this region."
    ),
    RATE_LIMITED: (
        "The upstream source is currently rate-limiting us. Try again in a few "
        "minutes."
    ),
    FETCH_FAILED: (
        "Couldn't reach this URL. It may be down, behind a login wall, or "
        "blocking automated requests."
    ),
    NO_TEXT_EXTRACTED: (
        "Reached the page but couldn't find readable article text. The site may "
        "be JS-only or paywalled."
    ),
    VIDEO_UNAVAILABLE: (
        "This video is unavailable — it may be private, deleted, or region-locked."
    ),
}


def user_message(reason: str | None, url: str = "") -> str:
    """Friendly explanation for a fetch failure, with a sensible fallback."""
    if reason and reason in _USER_MESSAGES:
        return _USER_MESSAGES[reason]
    if url:
        return f"Could not extract content from {url}."
    return "Could not extract content from the URL."
