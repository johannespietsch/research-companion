"""Per-user behaviour-signal digest fed into the analyzer (#69).

The product collects rich interaction signals — dismissals with free-text
reasons, now/later/never triage, tried/done outcomes — and until now fed none
of them back into `analyze()`. This module distils them into a short text
block injected into the analyze prompt next to the profile, so the 100th
analysis really is more *them* than the first.

Sources (backend SQLite only — the canonical event stream incl. anonymous
traffic stays in D1; the Worker forwards signed-in events to
``/api/suggestion-signals``):
- ``suggestion_signals``: dismiss events carry the user's own words for why a
  suggestion didn't fit ("too generic", "already done", …) — the highest-value
  signal we have.
- ``saved_suggestions``: completed/tried outcomes (with effort labels) and the
  size of the still-parked backlog.

Cache stability: the digest text is part of the analyze cache key, so a digest
that changed on every click would make the cache miss constantly. Only rows
from **before today 00:00 UTC** are included — the digest (and the key) is
stable within a UTC day, and learning lags reality by at most a day.

Anonymous users have no user_id here and always get an empty digest — their
analyses are unchanged (data-isolation invariant).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Hard bound — like the profile, this is injected into every analysis prompt.
SIGNAL_DIGEST_MAX_CHARS = 1200
# Look-back for dismissals; older taste signals go stale.
DISMISS_WINDOW_DAYS = 60
DISMISS_MAX = 6
OUTCOME_MAX = 6
# A backlog parked longer than this reads as overcommitment — nudge the model
# toward fewer/smaller steps rather than piling on.
PARKED_STALE_DAYS = 14


def _day_start(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT00:00:00")


def _quote(text: str, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def build_signal_digest(user_id: int | None, *, now: datetime | None = None) -> str:
    """Render the user's recent behaviour as a compact prompt block.

    Returns "" when there's no user or nothing learned yet — callers can drop
    the block entirely and the prompt is byte-identical to the pre-#69 one.
    """
    if user_id is None:
        return ""
    from bot.db import get_saved_suggestions, get_suggestion_signals

    now = now or datetime.now(timezone.utc)
    cutoff = _day_start(now)
    since = (now - timedelta(days=DISMISS_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")

    lines: list[str] = []

    dismissals = get_suggestion_signals(
        user_id, events=("dismiss",), before_iso=cutoff, since_iso=since,
        limit=DISMISS_MAX,
    )
    for d in dismissals:
        line = f'- Dismissed "{_quote(d["suggestion_text"])}"'
        if d["reason"]:
            line += f' — their reason: "{_quote(d["reason"])}"'
        lines.append(line)

    done_lines: list[str] = []
    parked_stale = 0
    stale_cutoff = (now - timedelta(days=PARKED_STALE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    for s in get_saved_suggestions(user_id):
        # Day-stable: ignore anything the user touched today.
        if s["updated_at"] >= cutoff:
            continue
        if s["status"] in ("done", "tried") and len(done_lines) < OUTCOME_MAX:
            verb = "Completed" if s["status"] == "done" else "Tried"
            effort = f' ({s["effort"]})' if s["effort"] else ""
            done_lines.append(f'- {verb} "{_quote(s["title"])}"{effort}')
        elif s["status"] == "saved" and s["created_at"] < stale_cutoff:
            parked_stale += 1
    lines.extend(done_lines)

    if parked_stale:
        lines.append(
            f"- {parked_stale} earlier suggestion{'s' if parked_stale != 1 else ''} "
            "still parked unactioned on their shortlist — favour fewer, smaller, "
            "more specific next steps over adding ambitious ones."
        )

    if not lines:
        return ""

    out = (
        "Recent behaviour signals from this person (use them to calibrate the "
        "suggestions — match what they complete, avoid what they dismiss; "
        "never mention these signals explicitly):\n" + "\n".join(lines)
    )
    return out[:SIGNAL_DIGEST_MAX_CHARS]
