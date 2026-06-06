"""Debug step 3: fetch + summarize + analyze a URL — the full pipeline ending in a verdict.

Uses the real `bot.pipeline.analyze_url`. By default runs as a signed-in
user (ctx.user_id=1) so the dispatch picks Sonnet 4.6 for summary +
analyze — same code path real signed-in users hit. Pass `--anon` to
simulate the anonymous /api/try caller (Haiku 4.5 throughout).

Surfaces all limits along the chain:
  - bot.config.MAX_CONTENT_CHARS    (fetched text cap)
  - bot.analyzer.SUMMARY_MAX_CHARS  (summary cap — analyze() runs on the summary, not the raw text)
  - profile text (DEFAULT_PROFILE if --profile not given; --profile reads from a file)

Run:
    python -m scripts.debug_verdict <url>                       # signed-in path (default)
    python -m scripts.debug_verdict <url> --anon                # anon /api/try path
    python -m scripts.debug_verdict <url> --profile PROFILE.md  # signed-in with a custom profile
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
        "--anon",
        action="store_true",
        help="Simulate the anonymous /api/try caller (no user_id in ctx). "
        "Default is signed-in (ctx.user_id=1) so the dispatch picks the "
        "premium model.",
    )
    parser.add_argument(
        "--profile",
        help="Path to a profile file (e.g. PROFILE.md). Omit to use DEFAULT_PROFILE.",
    )
    args = parser.parse_args()

    # Resolve which profile the analyzer will use. We always monkey-patch
    # _load_profile so analyze() picks up the configured text without
    # touching the real DB — independent of the tier flag.
    if args.profile:
        profile_text = Path(args.profile).read_text(encoding="utf-8").strip()
        profile_source = f"file: {args.profile}"
    else:
        profile_text = DEFAULT_PROFILE
        profile_source = "DEFAULT_PROFILE"
    analyzer._load_profile = lambda user_id=None: profile_text  # type: ignore[assignment]

    ctx = UsageContext(user_id=None if args.anon else 1)
    resolved_summary_model = analyzer._resolve_model("summary", ctx)
    resolved_analyze_model = analyzer._resolve_model("analyze", ctx)

    print()
    print("=== TIER ===")
    print(f"mode:           {'anon' if args.anon else 'signed-in (user_id=1)'}")
    print(f"summary model:  {resolved_summary_model}")
    print(f"analyze model:  {resolved_analyze_model}")

    print()
    print("=== PROFILE ===")
    print(f"source: {profile_source}")
    print(f"chars:  {len(profile_text):,}")
    print(profile_text)

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
