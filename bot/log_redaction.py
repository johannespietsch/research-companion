"""Strip bot tokens out of log records before they reach any handler.

httpx logs every request at INFO with the full URL, and every Telegram Bot API
call embeds the token in its path:

    httpx - INFO - HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage ...

That put a full-control credential (read all messages, send as the bot, reset
the webhook) in plaintext in `flyctl logs` on every outbound call, and in
anything downstream that ships or retains those logs.

We redact rather than silence httpx at WARNING: the request lines are genuinely
useful (feed fetches, Whisper calls, upstream failures), and redaction also
covers tokens that surface anywhere else — tracebacks, third-party libraries,
our own f-strings.
"""
from __future__ import annotations

import logging
import re


# Bot API URL path: /bot<id>:<secret>. Matched structurally so it works without
# knowing the configured token (dev tokens, rotated tokens, other bots).
_BOT_PATH = re.compile(r"/bot\d{5,}:[A-Za-z0-9_-]{20,}")

# Bare token, for the cases that aren't a URL path (config dumps, error text).
_BARE_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")

_REDACTED = "<redacted>"


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Replace anything token-shaped (and any literal secret) in `text`."""
    out = _BOT_PATH.sub(f"/bot{_REDACTED}", text)
    out = _BARE_TOKEN.sub(_REDACTED, out)
    for secret in extra_secrets:
        # Guard against empty/short values — replacing "" would shred the string.
        if secret and len(secret) >= 8:
            out = out.replace(secret, _REDACTED)
    return out


class RedactSecretsFilter(logging.Filter):
    """Rewrites token-bearing records in place.

    Attached to *handlers*, not loggers: a filter on the root logger is only
    consulted for records logged directly to it, and never for records
    propagated up from children like `httpx` — which is exactly the case we
    care about. Handler filters run for every record that reaches them.
    """

    def __init__(self, extra_secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._extra_secrets = tuple(s for s in extra_secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Malformed args are the formatter's problem, not ours — don't drop
            # the record, and don't crash logging.
            return True

        redacted = redact(message, self._extra_secrets)
        if redacted != message:
            # The secret usually lives in record.args (httpx logs the URL as a
            # %s arg), so rewriting record.msg alone would leave it intact.
            # Collapse to the already-formatted, redacted string.
            record.msg = redacted
            record.args = ()
        return True


def install(extra_secrets: tuple[str, ...] = ()) -> None:
    """Attach the filter to every handler on the root logger.

    Call after logging is configured and all handlers are attached. Safe to
    call multiple times.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactSecretsFilter) for f in handler.filters):
            handler.addFilter(RedactSecretsFilter(extra_secrets))
