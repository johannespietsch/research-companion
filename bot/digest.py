"""Weekly digest email (#67): the week's reads distilled into actions.

Assembled per user from their items of the last 7 days and sent every Friday
by the in-process loop in main.py (same single-machine constraint as the
other maintenance loops — the SQLite volume only attaches to one machine).

Design decisions:
- Actions lead, articles follow. The digest opens with up to MAX_ACTIONS
  suggestions from the week (watch-verdict items first), each with its
  agent-handoff brief — the digest is "do these things", not "read these
  links". Verdict is the lens-relevance ranking; no extra LLM calls.
- Sending goes straight to Resend from the backend. The Worker keeps its own
  Resend usage for magic-link mail; giving the backend its own key avoids a
  cross-service hop for a job that already lives here.
- Fail closed: without RESEND_API_KEY + DIGEST_FROM_EMAIL +
  DIGEST_UNSUBSCRIBE_SECRET nothing is sent — we never mail without a working
  unsubscribe link.
- Idempotent per week: users.digest_last_sent_at guards against a redeploy on
  send day re-mailing everyone.
"""
from __future__ import annotations

import hashlib
import hmac
import html as html_mod
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from bot.agent_brief import build_agent_brief
from bot.analyzer import parse_stored

logger = logging.getLogger(__name__)

MAX_ACTIONS = 3
# Don't resend if the last digest went out within this window — covers a
# restart later on send day without blocking next week's slot.
RESEND_GUARD_DAYS = 6

_RESEND_ENDPOINT = "https://api.resend.com/emails"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _base_url() -> str:
    """Public base for unsubscribe links. Defaults to the Fly app's public URL
    (WEBHOOK_URL), overridable via DIGEST_BASE_URL."""
    return (_env("DIGEST_BASE_URL") or _env("WEBHOOK_URL")).rstrip("/")


def configured() -> tuple[bool, str]:
    """(ok, reason). Sending requires the key, a From address, an unsubscribe
    secret, and a public base URL for the unsubscribe link."""
    if not _env("RESEND_API_KEY"):
        return False, "RESEND_API_KEY not set"
    if not _env("DIGEST_FROM_EMAIL"):
        return False, "DIGEST_FROM_EMAIL not set"
    if not _env("DIGEST_UNSUBSCRIBE_SECRET"):
        return False, "DIGEST_UNSUBSCRIBE_SECRET not set"
    if not _base_url():
        return False, "no public base URL (set DIGEST_BASE_URL or WEBHOOK_URL)"
    return True, ""


# ---------------------------------------------------------------------------
# Unsubscribe tokens — HMAC over the user id, so the link in the email can't
# be forged for another user and carries no PII.
# ---------------------------------------------------------------------------

def unsubscribe_token(user_id: int) -> str:
    secret = _env("DIGEST_UNSUBSCRIBE_SECRET").encode()
    msg = f"digest-unsub:{user_id}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def verify_unsubscribe_token(user_id: int, token: str) -> bool:
    if not _env("DIGEST_UNSUBSCRIBE_SECRET") or not token:
        return False
    return hmac.compare_digest(unsubscribe_token(user_id), token)


def unsubscribe_url(user_id: int) -> str:
    return (
        f"{_base_url()}/digest/unsubscribe"
        f"?uid={user_id}&tok={quote(unsubscribe_token(user_id))}"
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

_VERDICT_RANK = {"watch": 0, "skim": 1, "skip": 2}


def _item_label(analysis: dict, source: str) -> str:
    """Short human label for an item: its main idea, else the source URL.
    (items don't store a title — same approach as the /api/library list.)"""
    label = (analysis.get("main_idea") or "").strip()
    return label[:120] if label else (source or "untitled")


def build_digest(user_id: int, *, now: datetime | None = None) -> dict | None:
    """Assemble one user's digest over the trailing 7 days.

    Returns None when there's nothing to say (no items this week) — those
    users get no email rather than an empty one.
    """
    from bot.db import count_saved_suggestions_pending, get_items_since, get_user_profile

    now = now or datetime.now(timezone.utc)
    since_iso = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    items = get_items_since(user_id, since_iso)
    if not items:
        return None

    profile = get_user_profile(user_id)
    parsed = []  # (rank, item_row, analysis_dict)
    counts = {"items": len(items), "watch": 0, "skim": 0, "skip": 0}
    for row in items:
        analysis = parse_stored(row["analysis"]) or {}
        verdict = (analysis.get("verdict") or "").lower()
        if verdict in counts:
            counts[verdict] += 1
        parsed.append((_VERDICT_RANK.get(verdict, 1), row, analysis))
    parsed.sort(key=lambda t: (t[0], -t[1]["id"]))  # watch first, newest first

    # Top actions: the best suggestion of each watch/skim item, best-first
    # (suggestions[] is already ordered best-first by the analyzer), one per
    # source so a single dense read doesn't crowd out the rest of the week.
    # Convergent suggestions across items collapse into one action backed by
    # all of its sources (#70) — N reads → 1 thing to do.
    from bot.consolidate import cluster

    candidates = []
    for rank, row, analysis in parsed:
        if rank >= _VERDICT_RANK["skip"]:
            continue
        suggestions = analysis.get("suggestions") or []
        if not suggestions:
            continue
        s = suggestions[0]
        candidates.append({
            "title": (s.get("title") or "Try this").strip(),
            "detail": (s.get("detail") or "").strip(),
            "effort": (s.get("effort") or "").strip(),
            "first_step": (s.get("first_step") or "").strip(),
            "grounded_in": (analysis.get("grounded_in") or "").strip(),
            "source": row["source"],
            "source_label": _item_label(analysis, row["source"]),
        })

    actions = []
    for group in cluster(candidates)[:MAX_ACTIONS]:
        lead = group[0]  # candidates are rank-ordered, so the lead is the best take
        extra = [
            {"title": m["source_label"], "url": m["source"]}
            for m in group[1:]
        ]
        actions.append({
            "title": lead["title"],
            "detail": lead["detail"],
            "effort": lead["effort"],
            "first_step": lead["first_step"],
            "source": lead["source"],
            "source_label": lead["source_label"],
            "also_from": extra,
            "brief": build_agent_brief(
                action=lead["detail"] or lead["title"],
                first_step=lead["first_step"],
                grounded_in=lead["grounded_in"],
                profile=profile,
                source_title=lead["source_label"],
                source_url=lead["source"],
                extra_sources=extra,
                variant="link",
            ),
        })

    skipped = [
        {"label": _item_label(analysis, row["source"]), "source": row["source"]}
        for rank, row, analysis in parsed
        if rank == _VERDICT_RANK["skip"]
    ]

    return {
        "user_id": user_id,
        "counts": counts,
        "actions": actions,
        "skipped": skipped,
        "parked_count": count_saved_suggestions_pending(user_id),
    }


def digest_subject(d: dict) -> str:
    n, m = d["counts"]["items"], len(d["actions"])
    if m:
        return f"Your week, filtered — {n} read{'s' if n != 1 else ''}, {m} worth acting on"
    return f"Your week, filtered — {n} read{'s' if n != 1 else ''}"


# ---------------------------------------------------------------------------
# Rendering — plain text first (it's the canonical version), plus a minimal
# HTML twin. No images, no tracking pixels.
# ---------------------------------------------------------------------------

def render_digest_text(d: dict) -> str:
    c = d["counts"]
    out = [
        "filter.fyi — your week, filtered",
        "",
        f"{c['items']} reads this week · {c['watch']} worth the time · "
        f"{c['skim']} worth a skim · {c['skip']} skipped for you",
    ]
    if d["actions"]:
        out += ["", "DO THIS — the week in next steps", ""]
        for i, a in enumerate(d["actions"], 1):
            head = f"{i}. {a['title']}" + (f"  [{a['effort']}]" if a["effort"] else "")
            out.append(head)
            if a["detail"]:
                out.append(f"   {a['detail']}")
            if a["first_step"]:
                out.append(f"   First move: {a['first_step']}")
            out.append(f"   From: {a['source_label']} — {a['source']}")
            for extra in a.get("also_from") or []:
                out.append(f"   Also recommended by: {extra['title']} — {extra['url']}")
            if a["brief"]:
                out += ["", "   Hand this to your AI (ChatGPT, Claude, Cursor …):", ""]
                out += ["   | " + line for line in a["brief"].splitlines()]
            out.append("")
    else:
        out += ["", "Nothing demanded action this week — staying informed was enough."]
    if d["skipped"]:
        out += ["", "FILTERED OUT — reads we kept off your plate"]
        out += [f"- {s['label']}" for s in d["skipped"]]
    if d["parked_count"]:
        out += ["", f"Still parked on your shortlist: {d['parked_count']} suggestion"
                    f"{'s' if d['parked_count'] != 1 else ''} → https://filter.fyi/me#shortlist"]
    out += [
        "",
        "—",
        "filter.fyi — relevant, not noise.",
        f"Unsubscribe from the weekly digest: {unsubscribe_url(d['user_id'])}",
    ]
    return "\n".join(out)


def render_digest_html(d: dict) -> str:
    esc = html_mod.escape
    c = d["counts"]
    parts = [
        '<div style="font-family:ui-monospace,Menlo,monospace;max-width:640px;'
        'margin:0 auto;color:#1c1c1a;font-size:14px;line-height:1.6;">',
        '<p style="font-weight:700;">filter.fyi — your week, filtered</p>',
        f"<p>{c['items']} reads this week · {c['watch']} worth the time · "
        f"{c['skim']} worth a skim · {c['skip']} skipped for you</p>",
    ]
    if d["actions"]:
        parts.append('<p style="font-weight:700;letter-spacing:0.08em;">DO THIS — the week in next steps</p>')
        for a in d["actions"]:
            effort = f" <span style=\"color:#1f7a3a;\">[{esc(a['effort'])}]</span>" if a["effort"] else ""
            parts.append(
                '<div style="border:1px solid #1f7a3a;background:#dfe8d6;padding:12px;margin:10px 0;">'
                f"<p style=\"font-weight:700;margin:0 0 6px;\">{esc(a['title'])}{effort}</p>"
                + (f"<p style=\"margin:0 0 6px;\">{esc(a['detail'])}</p>" if a["detail"] else "")
                + (f"<p style=\"margin:0 0 6px;\">First move: {esc(a['first_step'])}</p>" if a["first_step"] else "")
                + f"<p style=\"margin:0 0 6px;font-size:12px;color:#5e5e58;\">From: "
                  f"<a href=\"{esc(a['source'], quote=True)}\">{esc(a['source_label'])}</a></p>"
                + "".join(
                    f"<p style=\"margin:0 0 6px;font-size:12px;color:#5e5e58;\">Also recommended by: "
                    f"<a href=\"{esc(x['url'], quote=True)}\">{esc(x['title'])}</a></p>"
                    for x in (a.get("also_from") or [])
                )
                + (
                    "<p style=\"margin:8px 0 4px;font-size:12px;color:#5e5e58;\">"
                    "Hand this to your AI (ChatGPT, Claude, Cursor …):</p>"
                    f"<pre style=\"white-space:pre-wrap;border:1px solid #1c1c1a;background:#f7f4ec;"
                    f"padding:8px;font-size:12px;\">{esc(a['brief'])}</pre>"
                    if a["brief"] else ""
                )
                + "</div>"
            )
    else:
        parts.append("<p>Nothing demanded action this week — staying informed was enough.</p>")
    if d["skipped"]:
        parts.append('<p style="font-weight:700;letter-spacing:0.08em;">FILTERED OUT — reads we kept off your plate</p><ul>')
        parts += [f"<li>{esc(s['label'])}</li>" for s in d["skipped"]]
        parts.append("</ul>")
    if d["parked_count"]:
        parts.append(
            f"<p>Still parked on your shortlist: {d['parked_count']} — "
            '<a href="https://filter.fyi/me#shortlist">review them</a>.</p>'
        )
    unsub = esc(unsubscribe_url(d["user_id"]), quote=True)
    parts.append(
        '<p style="font-size:11px;color:#9b9b91;border-top:1px solid #1c1c1a;padding-top:10px;">'
        "filter.fyi — relevant, not noise. · "
        f'<a href="{unsub}" style="color:#9b9b91;">unsubscribe from the weekly digest</a></p></div>'
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_digest_email(*, to: str, subject: str, text: str, html: str, user_id: int) -> None:
    """One Resend API call. Raises on non-2xx so the caller can count errors."""
    payload = {
        "from": _env("DIGEST_FROM_EMAIL"),
        "to": [to],
        "subject": subject,
        "text": text,
        "html": html,
        "headers": {"List-Unsubscribe": f"<{unsubscribe_url(user_id)}>"},
    }
    # Route replies to a real, monitored inbox (e.g. a Cloudflare Email
    # Routing address that forwards privately). The unsubscribe page and the
    # digest footer both invite replies, so set this in prod.
    reply_to = _env("DIGEST_REPLY_TO_EMAIL")
    if reply_to:
        payload["reply_to"] = reply_to
    resp = httpx.post(
        _RESEND_ENDPOINT,
        json=payload,
        headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}"},
        timeout=20,
    )
    resp.raise_for_status()


def _sent_recently(last_sent_iso: str, now: datetime) -> bool:
    if not last_sent_iso:
        return False
    try:
        last = datetime.strptime(last_sent_iso, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now - last < timedelta(days=RESEND_GUARD_DAYS)


def run_weekly_digest(*, now: datetime | None = None) -> dict:
    """Send the digest to every eligible user. Sync — the loop in main.py runs
    it via asyncio.to_thread. One user's failure never aborts the run."""
    from bot.db import get_digest_recipients, mark_digest_sent

    ok, reason = configured()
    if not ok:
        logger.warning("weekly digest skipped: %s", reason)
        return {"sent": 0, "skipped_empty": 0, "skipped_recent": 0, "errors": 0,
                "disabled_reason": reason}

    now = now or datetime.now(timezone.utc)
    stats = {"sent": 0, "skipped_empty": 0, "skipped_recent": 0, "errors": 0}
    for user in get_digest_recipients():
        try:
            if _sent_recently(user["digest_last_sent_at"], now):
                stats["skipped_recent"] += 1
                continue
            digest = build_digest(user["id"], now=now)
            if digest is None:
                stats["skipped_empty"] += 1
                continue
            send_digest_email(
                to=user["email"],
                subject=digest_subject(digest),
                text=render_digest_text(digest),
                html=render_digest_html(digest),
                user_id=user["id"],
            )
            mark_digest_sent(user["id"])
            stats["sent"] += 1
        except Exception:
            stats["errors"] += 1
            logger.exception("weekly digest failed for user %s", user["id"])
    logger.info("weekly digest run: %s", stats)
    return stats
