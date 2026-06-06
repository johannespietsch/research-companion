"""Debug step 3: fetch + summarize + analyze a URL — the full pipeline ending in a verdict.

Uses the real `bot.pipeline.analyze_url`, so this is exactly what `/api/try`
runs for an anonymous user (no `save_for_user_id`).

Surfaces all limits along the chain:
  - bot.config.MAX_CONTENT_CHARS    (fetched text cap)
  - bot.analyzer.SUMMARY_MAX_CHARS  (summary cap — analyze() runs on the summary, not the raw text)
  - profile text (DEFAULT_PROFILE for anon, or the file passed via --profile)

Run:
    python -m scripts.debug_verdict <url>                       # anon → DEFAULT_PROFILE
    python -m scripts.debug_verdict <url> --profile PROFILE.md  # use a custom profile file
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("debug.verdict")


def main() -> int:
    from bot import analyzer
    from bot.analyzer import DEFAULT_PROFILE, SUMMARY_MAX_CHARS, UsageContext
    from bot.config import MAX_CONTENT_CHARS
    from bot.pipeline import PipelineError, analyze_url

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument(
        "--profile",
        help="Path to a profile file (e.g. PROFILE.md). Omit to use DEFAULT_PROFILE (anon).",
    )
    args = parser.parse_args()

    # Resolve which profile the analyzer will use. We monkey-patch
    # _load_profile so analyze() picks up the supplied file without needing a
    # signed-in user_id, mirroring the anon path otherwise.
    if args.profile:
        profile_text = Path(args.profile).read_text(encoding="utf-8").strip()
        profile_source = f"file: {args.profile}"
        analyzer._load_profile = lambda user_id=None: profile_text  # type: ignore[assignment]
    else:
        profile_text = DEFAULT_PROFILE
        profile_source = "DEFAULT_PROFILE (anon)"

    print()
    print("=== PROFILE ===")
    print(f"source: {profile_source}")
    print(f"chars:  {len(profile_text):,}")
    print(profile_text)

    ctx = UsageContext()  # anon: no user_id / anon_id / job_id

    def on_step(label: str) -> None:
        logger.info("pipeline step: %s", label)

    try:
        result = asyncio.run(analyze_url(args.url, ctx=ctx, on_step=on_step))
    except PipelineError as e:
        print(f"pipeline error: {e.code} — {e}", file=sys.stderr)
        return 1

    fetched_text = result.fetched.get("text") or ""

    print()
    print("=== LIMITS / SIZES ===")
    print(f"MAX_CONTENT_CHARS:    {MAX_CONTENT_CHARS:,}")
    print(f"fetched text chars:   {len(fetched_text):,}"
          f"{'  (TRUNCATED)' if len(fetched_text) >= MAX_CONTENT_CHARS else ''}")
    print(f"SUMMARY_MAX_CHARS:    {SUMMARY_MAX_CHARS:,}")
    print(f"summary chars:        {len(result.summary):,}"
          f"{'  (TRUNCATED)' if len(result.summary) >= SUMMARY_MAX_CHARS else ''}")
    print(f"  (analyze() runs on the summary, not the raw fetched text)")

    print()
    print("=== SUMMARY (input to analyze) ===")
    print(result.summary)

    print()
    print("=== ANALYSIS ===")
    print(json.dumps(result.analysis, ensure_ascii=False, indent=2))

    print()
    print(f"VERDICT: {result.analysis.get('verdict')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
