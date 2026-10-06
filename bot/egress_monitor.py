"""Watch the YouTube egress proxy (the home MacBook over Tailscale, #128) and
DM the admin on Telegram when it goes down, stays down, and recovers.

YouTube blocks Fly's IP, so while the Mac is unreachable every YouTube link
falls back to direct and fails with no-transcript. The per-fetch probe in
`bot.fetcher` only runs when someone submits a link; this checks on a timer so
the admin hears about an outage before a user does.

`EgressMonitor` is the pure state machine (fed `ok` + a clock) so the alert
rules are testable without timers or network; `check_once` wires it to the
real probe and sender.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Down for at least this long before the first alert: one failed check is
# usually a Wi-Fi blip or a Mac waking up, not an outage.
DOWN_ALERT_AFTER_S = 10 * 60
# While still down, remind at this interval.
REMINDER_EVERY_S = 6 * 3600


def _fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


class EgressMonitor:
    """Turns a stream of probe results into at most one message per check."""

    def __init__(self) -> None:
        self.first_fail_at: float | None = None   # monotonic
        self.first_fail_wall: datetime | None = None
        self.alerted = False
        self.last_alert_at: float | None = None

    def observe(self, ok: bool, now: float, *, fallbacks_since=None) -> str | None:
        """Record one probe result; return the message to send, if any.

        `fallbacks_since(wall_dt) -> int` counts YouTube fetches that fell
        back to direct since the outage began (only called on recovery)."""
        if ok:
            if not self.alerted:
                self._reset()  # a blip that never reached the alert threshold
                return None
            down_for = now - self.first_fail_at
            n = fallbacks_since(self.first_fail_wall) if fallbacks_since else 0
            self._reset()
            return (
                f"✅ YouTube egress recovered after {_fmt_duration(down_for)}. "
                f"{n} YouTube link{'s' if n != 1 else ''} went direct (and likely "
                f"failed) meanwhile."
            )

        if self.first_fail_at is None:
            self.first_fail_at = now
            self.first_fail_wall = datetime.now(timezone.utc)
        down_for = now - self.first_fail_at

        if not self.alerted:
            if down_for < DOWN_ALERT_AFTER_S:
                return None
            self.alerted = True
            self.last_alert_at = now
            return (
                f"⚠️ YouTube egress is DOWN (for {_fmt_duration(down_for)}): the "
                f"home MacBook exit node isn't reachable from Fly, so YouTube links "
                f"fail with no-transcript. Check the Mac is on, awake and on "
                f"Tailscale."
            )

        if now - self.last_alert_at >= REMINDER_EVERY_S:
            self.last_alert_at = now
            return f"⚠️ YouTube egress still DOWN ({_fmt_duration(down_for)} so far)."
        return None

    def _reset(self) -> None:
        self.first_fail_at = None
        self.first_fail_wall = None
        self.alerted = False
        self.last_alert_at = None


def send_admin_telegram(text: str) -> bool:
    """DM ADMIN_TELEGRAM_CHAT_ID via the bot. Never logs the URL or the
    exception text — both contain the bot token."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("ADMIN_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info("admin alert not sent (TELEGRAM_TOKEN/ADMIN_TELEGRAM_CHAT_ID unset): %s", text)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        logger.warning("admin alert send failed (%s)", type(e).__name__)
        return False
    if resp.status_code != 200:
        logger.warning("admin alert send failed (HTTP %s)", resp.status_code)
        return False
    return True


def _fallbacks_since(wall: datetime | None) -> int:
    from bot.db import count_processed_urls_by_egress
    from bot.fetcher import EGRESS_DIRECT_FALLBACK

    if wall is None:
        return 0
    return count_processed_urls_by_egress(
        EGRESS_DIRECT_FALLBACK, wall.strftime("%Y-%m-%dT%H:%M:%S")
    )


def check_once(monitor: EgressMonitor) -> str | None:
    """Probe, feed the monitor, send whatever it says. Blocking — run it in a
    thread. Returns the message sent (for logging/tests)."""
    from bot.fetcher import probe_youtube_proxy

    msg = monitor.observe(
        probe_youtube_proxy(), time.monotonic(), fallbacks_since=_fallbacks_since
    )
    if msg:
        # Down/still-down are warnings (→ error_log → admin dashboard);
        # recovery is informational.
        level = logging.INFO if monitor.first_fail_at is None else logging.WARNING
        logger.log(level, "egress monitor: %s", msg)
        send_admin_telegram(msg)
    return msg
