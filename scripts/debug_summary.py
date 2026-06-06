"""Debug step 2: fetch + summarize a URL through the real pipeline.

Surfaces the limits applied at this stage:
  - bot.config.MAX_CONTENT_CHARS — fetched text is truncated to this before
    summarization sees it.
  - bot.analyzer.SUMMARY_MAX_CHARS — final stored summary is capped here.
  - bot.analyzer._SUMMARY_MAX_OUTPUT_TOKENS — hard ceiling on requested output.
  - bot.analyzer._summary_output_tokens(text) — actual max_tokens requested,
    scaled to the input length.

Hits the same content-addressed cache as the production pipeline; a "cache hit"
log line means no LLM call was made.

Run:
    python -m scripts.debug_summary <url>
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("debug.summary")


def main() -> int:
    from bot import analyzer
    from bot.analyzer import (
        SUMMARY_MAX_CHARS,
        UsageContext,
        _SUMMARY_MAX_OUTPUT_TOKENS,
        _summary_output_tokens,
        summarize_content,
    )
    from bot.config import MAX_CONTENT_CHARS
    from bot.fetcher import fetch_url

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force a fresh LLM call (the summary cache key doesn't include "
        "max_tokens, so a stale truncated result would otherwise be returned).",
    )
    args = parser.parse_args()

    if args.no_cache:
        analyzer._try_cache_get = lambda _key: None  # type: ignore[assignment]
        logger.info("--no-cache: summary cache lookups disabled for this run")

    fetched = asyncio.run(fetch_url(args.url))
    text = (fetched.get("text") or "").strip()
    if not text:
        print(f"no extractable text (reason={fetched.get('reason')})", file=sys.stderr)
        return 1

    requested_max_tokens = _summary_output_tokens(text)

    print()
    print("=== FETCH ===")
    print(f"source_type:        {fetched.get('source_type')}")
    print(f"title:              {fetched.get('title')}")
    print(f"fetched text chars: {len(text):,}  (MAX_CONTENT_CHARS={MAX_CONTENT_CHARS:,})")
    if len(text) >= MAX_CONTENT_CHARS:
        print(f"*** input was TRUNCATED at MAX_CONTENT_CHARS before summarization ***")

    print()
    print("=== SUMMARIZE LIMITS ===")
    print(f"SUMMARY_MAX_CHARS:        {SUMMARY_MAX_CHARS:,}  (final cap on summary text)")
    print(f"_SUMMARY_MAX_OUTPUT_TOKENS:{_SUMMARY_MAX_OUTPUT_TOKENS:>7,}  (hard ceiling)")
    print(f"requested max_tokens:     {requested_max_tokens:,}  (scaled: ~input_chars // 4, 1:1 with input tokens)")

    ctx = UsageContext(source_type=fetched.get("source_type") or "")
    summary = summarize_content(text, ctx=ctx)

    # Estimate output tokens used to detect a max_tokens-shaped cutoff. The
    # char cap (SUMMARY_MAX_CHARS) and the token cap (requested max_tokens)
    # bound the output independently — the token cap is the binding
    # constraint for long inputs and shows up as a summary that ends
    # mid-word / mid-section, even though `len(summary) < SUMMARY_MAX_CHARS`.
    approx_out_tokens = len(summary) // 4
    token_fill = approx_out_tokens / requested_max_tokens if requested_max_tokens else 0

    print()
    print("=== SUMMARY ===")
    print(f"summary chars:        {len(summary):,}")
    print(f"approx out tokens:    {approx_out_tokens:,}  (len // 4)")
    print(f"requested max_tokens: {requested_max_tokens:,}  (fill ratio: {token_fill:.1%})")
    if len(summary) >= SUMMARY_MAX_CHARS:
        print(f"*** summary TRUNCATED at SUMMARY_MAX_CHARS ({SUMMARY_MAX_CHARS:,}) ***")
    if token_fill >= 0.95:
        print(f"*** LIKELY TRUNCATED at requested max_tokens ({requested_max_tokens:,}) ***")
        print(f"    fix: bump _summary_output_tokens / _SUMMARY_MAX_OUTPUT_TOKENS in bot/analyzer.py")
    print()
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
