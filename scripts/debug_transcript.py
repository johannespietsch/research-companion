"""Debug step 1: fetch a URL through the real pipeline and show the transcript / extracted text.

Surfaces the internal limit applied at this stage:
  - bot.config.MAX_CONTENT_CHARS — fetched text is truncated to this length
    inside the YouTube/X/article fetchers before anything downstream sees it.

For YouTube URLs we also pull the raw transcript directly via
YouTubeTranscriptApi so the script can show the *pre-truncation* length and
make the cut-off visible.

Run:
    python -m scripts.debug_transcript <url>
"""
from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("debug.transcript")


def _raw_youtube_transcript_len(url: str) -> int | None:
    """Pre-truncation raw transcript length, or None if not YouTube / unavailable.

    Mirrors `_youtube_transcript`'s language-agnostic selection so it works for
    non-English videos too — preferring manual over auto-generated."""
    from bot.fetcher import _YT_PATTERNS

    match = _YT_PATTERNS.search(url)
    if not match:
        return None
    video_id = match.group(1)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
        transcripts = list(api.list(video_id))
        manual = next((t for t in transcripts if not t.is_generated), None)
        generated = next((t for t in transcripts if t.is_generated), None)
        transcript = manual or generated
        if transcript is None:
            return None
        fetched = transcript.fetch()
        return sum(len(s.text) + 1 for s in fetched)  # +1 mirrors the " ".join
    except Exception as e:
        logger.warning("raw transcript fetch failed: %s", e)
        return None


def main() -> int:
    import asyncio

    from bot.config import MAX_CONTENT_CHARS
    from bot.fetcher import fetch_url

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args()

    raw_len = _raw_youtube_transcript_len(args.url)
    fetched = asyncio.run(fetch_url(args.url))

    text = fetched.get("text") or ""
    print()
    print("=== FETCH ===")
    print(f"url:          {args.url}")
    print(f"source_type:  {fetched.get('source_type')}")
    print(f"title:        {fetched.get('title')}")
    print(f"image_urls:   {len(fetched.get('image_urls') or [])}")
    if fetched.get("language"):
        print(f"language:     {fetched['language']}")
    if fetched.get("reason"):
        print(f"reason:       {fetched['reason']}")

    print()
    print("=== LIMITS ===")
    print(f"MAX_CONTENT_CHARS:    {MAX_CONTENT_CHARS:,}  (applied inside fetcher)")
    if raw_len is not None:
        print(f"raw transcript chars: {raw_len:,}  (pre-truncation, via YouTubeTranscriptApi)")
    print(f"fetched text chars:   {len(text):,}")
    if len(text) >= MAX_CONTENT_CHARS:
        print(f"*** TRUNCATED at MAX_CONTENT_CHARS ({MAX_CONTENT_CHARS:,}) ***")
    if raw_len is not None and raw_len > MAX_CONTENT_CHARS:
        # rough — fetched text includes "<title>\n\nTranscript:\n" prefix too
        print(f"*** ~{raw_len - MAX_CONTENT_CHARS:,} chars of raw transcript dropped ***")

    print()
    print("=== TEXT ===")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
