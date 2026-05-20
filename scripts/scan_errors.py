"""Daily error-log scanner — classifies WARNING+ records and files GH issues for bugs.

Reads the `error_log` table populated by bot.error_logging, groups by
fingerprint, asks Claude to bucket each group as
  - known_user_limit  → already covered by bot/fetch_errors.py, ignore
  - bug               → file a GH issue (dedup by fingerprint marker)
  - noise             → ignore

Run locally:
    GH_TOKEN=ghp_... ANTHROPIC_API_KEY=... python -m scripts.scan_errors --dry-run

On Fly (scheduled machine):
    python -m scripts.scan_errors
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

# Import bot.* AFTER the env is loaded so DATA_DIR resolves correctly.
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scan_errors")


# Reasons in fetch_errors.py are already-handled user limitations. If a warning
# correlates with one of these, Claude should classify it as known_user_limit.
_KNOWN_USER_REASONS_HINT = [
    "no_transcript", "video_too_long_for_whisper", "whisper_failed",
    "streamyard_intercept_failed", "image_only_pdf", "pdf_download_failed",
    "tweet_unavailable", "rate_limited", "fetch_failed", "no_text_extracted",
    "video_unavailable",
]


GH_REPO = os.getenv("GH_REPO", "johannespietsch/research-companion")


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _group_errors(rows: list) -> list[dict]:
    """Group rows by fingerprint, return summaries sorted by count desc."""
    groups: dict[str, dict] = {}
    for r in rows:
        fp = r["fingerprint"]
        g = groups.setdefault(fp, {
            "fingerprint": fp,
            "logger": r["logger"],
            "level": r["level"],
            "count": 0,
            "first_ts": r["ts"],
            "last_ts": r["ts"],
            "sample_message": r["message"],
            "sample_traceback": r["traceback"],
            "loggers": Counter(),
            "messages": [],
        })
        g["count"] += 1
        g["loggers"][r["logger"]] += 1
        if r["ts"] < g["first_ts"]:
            g["first_ts"] = r["ts"]
        if r["ts"] > g["last_ts"]:
            g["last_ts"] = r["ts"]
        if len(g["messages"]) < 3 and r["message"] not in g["messages"]:
            g["messages"].append(r["message"])
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)


_CLASSIFY_PROMPT = """\
You are triaging warnings/errors from filter.fyi (a Telegram + web research bot).
Categorize each error group as exactly one of:

- "known_user_limit": expected user-facing failure already handled by
  bot/fetch_errors.py reason codes ({reasons}). The bot already shows the user
  a friendly explanation. Examples: PDF download failed, video too long for
  Whisper, YouTube rate-limited, tweet unavailable.

- "bug": unexpected exception or breakage that needs developer attention.
  Examples: AttributeError, KeyError, database integrity error, "url_cache
  write failed", code that hits a branch it shouldn't.

- "noise": logs that aren't actionable. Examples: temporary network blips,
  client-side disconnects, expected `Blocked user` warnings, image analysis
  failures on individual social-media images (best-effort already).

For "bug" groups also draft:
  - "issue_title": short imperative ("Fix X when Y")
  - "issue_body": one paragraph explanation referencing the affected file/logger
    and what to investigate. Do NOT include the fingerprint — the caller adds it.

Respond with a JSON array, one object per input group, same order, with keys:
  fingerprint, classification, issue_title (optional), issue_body (optional).
"""


def _classify(groups: list[dict]) -> list[dict]:
    """Send error groups to Claude, parse classifications back."""
    if not groups:
        return []
    import anthropic

    client = anthropic.Anthropic()
    payload = [
        {
            "fingerprint": g["fingerprint"],
            "logger": g["logger"],
            "level": g["level"],
            "count": g["count"],
            "sample_messages": g["messages"],
            "sample_traceback": g["sample_traceback"][:2000],
        }
        for g in groups
    ]
    prompt = _CLASSIFY_PROMPT.format(reasons=", ".join(_KNOWN_USER_REASONS_HINT))

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        system=prompt,
        messages=[{
            "role": "user",
            "content": "Groups to classify:\n\n" + json.dumps(payload, indent=2),
        }],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if text.startswith("json"):
        text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Could not parse classifier response as JSON: %s\n--- raw ---\n%s", e, text)
        return [{"fingerprint": g["fingerprint"], "classification": "noise"} for g in groups]

    by_fp = {item["fingerprint"]: item for item in parsed if isinstance(item, dict)}
    return [by_fp.get(g["fingerprint"], {"fingerprint": g["fingerprint"], "classification": "noise"}) for g in groups]


def _gh_search_existing(fingerprint: str, token: str | None) -> int | None:
    """Find an open issue with the fingerprint marker. Returns issue number or None."""
    if not token:
        return None
    marker = f"<!-- fingerprint: {fingerprint} -->"
    q = f'repo:{GH_REPO} is:issue is:open in:body "{marker}"'
    resp = httpx.get(
        "https://api.github.com/search/issues",
        params={"q": q},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    if resp.status_code != 200:
        log.warning("GH search failed (%s): %s", resp.status_code, resp.text[:200])
        return None
    items = resp.json().get("items", [])
    return items[0]["number"] if items else None


def _gh_create_issue(title: str, body: str, token: str | None) -> int | None:
    if not token:
        log.warning("GitHub App not configured — would create issue: %s", title)
        return None
    resp = httpx.post(
        f"https://api.github.com/repos/{GH_REPO}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body, "labels": ["bug", "auto-filed"]},
        timeout=20,
    )
    if resp.status_code >= 300:
        log.error("GH create issue failed (%s): %s", resp.status_code, resp.text[:400])
        return None
    return resp.json().get("number")


def _build_issue_body(group: dict, draft: dict) -> str:
    drafted = draft.get("issue_body") or "Auto-filed from production logs."
    body = (
        f"<!-- fingerprint: {group['fingerprint']} -->\n\n"
        f"{drafted}\n\n"
        f"**Logger:** `{group['logger']}`  \n"
        f"**Level:** {group['level']}  \n"
        f"**Occurrences (last 24h):** {group['count']}  \n"
        f"**First seen:** {group['first_ts']}  \n"
        f"**Last seen:** {group['last_ts']}\n\n"
        f"### Sample messages\n```\n"
        + "\n---\n".join(group["messages"])
        + "\n```\n"
    )
    if group["sample_traceback"]:
        body += (
            "\n### Sample traceback\n```\n"
            + group["sample_traceback"][:3000]
            + "\n```\n"
        )
    body += "\n_Filed automatically by `scripts/scan_errors.py`._"
    return body


def run_scan(*, since_hours: int = 24, dry_run: bool = False,
             prune_older_than_days: int = 14) -> dict:
    """Programmatic entry point — used by the in-process scheduler and the CLI."""
    from bot.db import get_recent_errors, prune_error_log

    since = _since_iso(since_hours)
    rows = get_recent_errors(since)
    log.info("Found %d error_log rows since %s", len(rows), since)

    groups = _group_errors(rows)
    log.info("Grouped into %d fingerprints", len(groups))

    filed = 0
    skipped_existing = 0

    if groups:
        classifications = _classify(groups)

        # Mint one installation token per run only if there's a bug to act on.
        token: str | None = None
        if any(c.get("classification") == "bug" for c in classifications):
            from scripts import github_app
            if github_app.configured():
                try:
                    token = github_app.installation_token(GH_REPO)
                except Exception:
                    log.exception("Failed to mint GitHub App installation token")
            else:
                log.warning("GitHub App not configured (GH_APP_ID / GH_APP_PRIVATE_KEY)")

        for group, cls in zip(groups, classifications):
            verdict = cls.get("classification", "noise")
            log.info("[%s] %s (%dx) %s", verdict, group["fingerprint"], group["count"],
                     group["messages"][0][:80])
            if verdict != "bug":
                continue

            existing = _gh_search_existing(group["fingerprint"], token)
            if existing:
                log.info("  → already tracked in issue #%d, skipping", existing)
                skipped_existing += 1
                continue

            title = cls.get("issue_title") or f"Investigate {group['logger']}: {group['messages'][0][:60]}"
            body = _build_issue_body(group, cls)

            if dry_run:
                log.info("  → DRY RUN: would file issue %r", title)
                continue

            number = _gh_create_issue(title, body, token)
            if number:
                log.info("  → filed issue #%d", number)
                filed += 1

    log.info("Done: filed=%d skipped_existing=%d total_groups=%d",
             filed, skipped_existing, len(groups))

    pruned = 0
    if prune_older_than_days > 0:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=prune_older_than_days)).strftime("%Y-%m-%dT%H:%M:%S")
        pruned = prune_error_log(cutoff)
        if pruned:
            log.info("Pruned %d error_log rows older than %s", pruned, cutoff)

    return {
        "groups": len(groups),
        "filed": filed,
        "skipped_existing": skipped_existing,
        "pruned": pruned,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify but don't create GH issues")
    parser.add_argument("--prune-older-than-days", type=int, default=14,
                        help="Delete error_log rows older than N days after scanning")
    args = parser.parse_args()

    run_scan(
        since_hours=args.since_hours,
        dry_run=args.dry_run,
        prune_older_than_days=args.prune_older_than_days,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
