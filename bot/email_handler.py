"""
Inbound email handler — compatible with Mailgun's inbound parse webhook.

Mailgun setup:
  1. Add a receiving route in Mailgun: match all → forward to https://your-domain/inbound-email
  2. In your profile, register your email address via the web UI or:
       python kb.py adduser you@example.com
     Then set the email field via the API or Telegram /profile.

Optional security: set MAILGUN_SIGNING_KEY in .env to enable HMAC-SHA256 signature
verification. See: https://documentation.mailgun.com/docs/mailgun/user-manual/webhook-security/
"""

import email.utils
import hashlib
import hmac
import logging
import os

from fastapi import Request

from bot.analyzer import analyze, to_json_str
from bot.config import MAX_CONTENT_CHARS
from bot.db import get_profile_by_email, save_item

logger = logging.getLogger(__name__)

_SIGNING_KEY = os.getenv("MAILGUN_SIGNING_KEY", "")


def _verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    """Verify Mailgun webhook signature. Returns True if valid or if no key configured."""
    if not _SIGNING_KEY:
        return True
    value = timestamp + token
    digest = hmac.new(_SIGNING_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


async def handle_inbound_email(request: Request) -> dict:
    form = await request.form()

    # Optional signature verification
    if _SIGNING_KEY:
        ok = _verify_mailgun_signature(
            token=form.get("token", ""),
            timestamp=form.get("timestamp", ""),
            signature=form.get("signature", ""),
        )
        if not ok:
            logger.warning("Mailgun signature verification failed")
            return {"ok": False, "reason": "invalid signature"}

    # Parse sender address
    raw_from = form.get("from", "")
    _, sender_email = email.utils.parseaddr(raw_from)
    sender_email = sender_email.lower().strip()

    if not sender_email:
        logger.warning("Inbound email with no parseable from address")
        return {"ok": False, "reason": "missing from address"}

    # Map email → user
    row = get_profile_by_email(sender_email)
    if not row:
        logger.info("Inbound email from unregistered address: %s (dropped)", sender_email)
        # Return success so Mailgun does not retry
        return {"ok": False, "reason": "unknown sender"}

    user_id = row["user_id"]
    subject = (form.get("subject") or "").strip()

    # Prefer stripped-text (no quoted replies), fall back to body-plain
    body = (form.get("stripped-text") or form.get("body-plain") or "").strip()
    if not body:
        return {"ok": False, "reason": "empty body"}

    if subject:
        body = f"Subject: {subject}\n\n{body}"

    body = body[:MAX_CONTENT_CHARS]

    try:
        analysis = analyze(body, user_id)
    except Exception:
        logger.exception("Analysis failed for inbound email from %s", sender_email)
        return {"ok": False, "reason": "analysis error"}

    save_item(
        user_id=user_id,
        source_type="email",
        source=sender_email,
        content=body,
        analysis=to_json_str(analysis),
        user_note=subject,
    )

    logger.info("Saved inbound email from %s for user %s", sender_email, user_id)
    return {"ok": True}
