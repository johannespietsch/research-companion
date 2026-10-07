"""Tests for bot.egress_monitor — the YouTube egress (home Mac) alerting, #128."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from bot.egress_monitor import (
    DOWN_ALERT_AFTER_S,
    REMINDER_EVERY_S,
    EgressMonitor,
    send_admin_telegram,
)

CHECK = 300  # the 5-minute interval


def _run(monitor, results, start=0.0, step=CHECK, **kw):
    """Feed probe results at a fixed cadence; return the non-None messages."""
    out = []
    for i, ok in enumerate(results):
        msg = monitor.observe(ok, start + i * step, **kw)
        if msg:
            out.append((i, msg))
    return out


class TestEgressMonitorRules:
    def test_healthy_stays_silent(self):
        assert _run(EgressMonitor(), [True] * 20) == []

    def test_short_blip_never_alerts(self):
        # One or two failed checks (< 10 min) then back: Wi-Fi blip, no DM.
        assert _run(EgressMonitor(), [True, False, False, True, True]) == []

    def test_alerts_once_down_for_ten_minutes(self):
        msgs = _run(EgressMonitor(), [False, False, False, False])
        assert len(msgs) == 1
        idx, text = msgs[0]
        assert idx * CHECK >= DOWN_ALERT_AFTER_S  # the third failed check
        assert "DOWN" in text

    def test_reminds_every_six_hours_while_down(self):
        checks = int((REMINDER_EVERY_S * 2 + DOWN_ALERT_AFTER_S) / CHECK) + 1
        msgs = _run(EgressMonitor(), [False] * checks)
        assert [("still DOWN" in t) for _, t in msgs] == [False, True, True]

    def test_recovery_message_with_duration_and_fallback_count(self):
        monitor = EgressMonitor()
        counted = []

        def fallbacks_since(wall):
            counted.append(wall)
            return 3

        msgs = _run(monitor, [False] * 7 + [True], fallbacks_since=fallbacks_since)
        assert len(msgs) == 2
        _, recovered = msgs[1]
        assert "recovered after 35 min" in recovered
        assert "3 YouTube links went direct" in recovered
        assert counted and counted[0] is not None

    def test_recovery_resets_so_next_outage_alerts_again(self):
        monitor = EgressMonitor()
        msgs = _run(monitor, [False] * 3 + [True] + [False] * 3)
        assert [("DOWN" in t, "recovered" in t) for _, t in msgs] == [
            (True, False), (False, True), (True, False),
        ]


class TestSendAdminTelegram:
    def test_skips_without_chat_id(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
        monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)
        with patch("bot.egress_monitor.requests.post") as post:
            assert send_admin_telegram("hi") is False
        post.assert_not_called()

    def test_posts_to_admin_chat(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
        monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "42")
        with patch("bot.egress_monitor.requests.post",
                   return_value=MagicMock(status_code=200)) as post:
            assert send_admin_telegram("hi") is True
        assert post.call_args.kwargs["json"] == {"chat_id": "42", "text": "hi"}

    def test_failure_never_logs_the_token(self, monkeypatch, caplog):
        # requests' exception text includes the URL, which contains the token.
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:SECRET-TOKEN")
        monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "42")
        boom = ConnectionError("https://api.telegram.org/bot123:SECRET-TOKEN/sendMessage")
        with patch("bot.egress_monitor.requests.post", side_effect=boom), \
             caplog.at_level(logging.DEBUG):
            assert send_admin_telegram("hi") is False
        assert "SECRET-TOKEN" not in caplog.text


class TestCheckOnce:
    def test_probe_result_drives_monitor_and_sends(self, monkeypatch):
        import time
        from bot import egress_monitor as em

        monitor = em.EgressMonitor()
        # Already failing for just over the alert threshold.
        monitor.observe(False, time.monotonic() - DOWN_ALERT_AFTER_S - 1)
        monkeypatch.setattr("bot.fetcher.probe_youtube_proxy", lambda: False)
        send = MagicMock()
        monkeypatch.setattr(em, "send_admin_telegram", send)
        msg = em.check_once(monitor)
        assert msg and "DOWN (for 10 min)" in msg
        send.assert_called_once_with(msg)


class TestFallbackCount:
    def test_counts_direct_fallback_rows_since(self, db):
        from bot.db import count_processed_urls_by_egress, record_processed_url

        record_processed_url(url="https://youtu.be/a", source_type="youtube", egress="direct-fallback")
        record_processed_url(url="https://youtu.be/b", source_type="youtube", egress="proxy")
        assert count_processed_urls_by_egress("direct-fallback", "2000-01-01T00:00:00") == 1
        assert count_processed_urls_by_egress("direct-fallback", "2999-01-01T00:00:00") == 0
