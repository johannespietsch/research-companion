"""Tests for Telegram webhook authentication and lifecycle in main.py.

The /webhook route is the one public, unauthenticated-by-default surface on the
backend, so it must reject any request that doesn't echo our secret token.

It must also ack with 200 *before* the handler chain completes — Telegram
retries any webhook call that doesn't ack within ~60s, so a slow handler
(video transcribe → analyze easily exceeds that) used to get the same update
redelivered and double-processed. The fire-and-forget tests below pin that.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def main_mod(monkeypatch):
    """Import main.py with Telegram disabled at import, then hand it back so
    tests can monkeypatch the module-level globals the route reads."""
    # No token at import => build_application() is skipped (no network/app build).
    monkeypatch.setenv("TELEGRAM_TOKEN", "")
    import main
    return main


class TestWebhookSecretCheck:
    def test_correct_token_accepted(self, main_mod, monkeypatch):
        monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", "s3cret")
        assert main_mod._webhook_secret_ok("s3cret") is True

    def test_wrong_token_rejected(self, main_mod, monkeypatch):
        monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", "s3cret")
        assert main_mod._webhook_secret_ok("nope") is False

    def test_missing_header_rejected(self, main_mod, monkeypatch):
        monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", "s3cret")
        assert main_mod._webhook_secret_ok(None) is False

    def test_no_configured_secret_fails_closed(self, main_mod, monkeypatch):
        monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", None)
        assert main_mod._webhook_secret_ok("anything") is False


class TestWebhookRoute:
    @pytest.fixture
    def client(self, main_mod, monkeypatch):
        # Wire up webhook mode with a stub Telegram app so the route runs.
        app_stub = MagicMock()
        app_stub.process_update = AsyncMock()
        monkeypatch.setattr(main_mod, "telegram_app", app_stub)
        monkeypatch.setattr(main_mod, "WEBHOOK_URL", "https://example.com")
        monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", "s3cret")
        monkeypatch.setattr(main_mod.Update, "de_json", lambda data, bot: {"ok": 1})
        return TestClient(main_mod.app), app_stub

    def test_valid_secret_processes_update(self, client, main_mod):
        c, app_stub = client
        r = c.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert r.status_code == 200
        # process_update runs on a background task created by the handler.
        # We have to drain the task set before asserting on the mock —
        # otherwise the assertion races the scheduled task.
        _drain_webhook_tasks(main_mod)
        app_stub.process_update.assert_awaited_once()

    def test_invalid_secret_rejected_without_processing(self, client):
        c, app_stub = client
        r = c.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert r.status_code == 403
        app_stub.process_update.assert_not_awaited()

    def test_missing_secret_header_rejected(self, client):
        c, app_stub = client
        r = c.post("/webhook", json={"update_id": 1})
        assert r.status_code == 403
        app_stub.process_update.assert_not_awaited()


class TestWebhookFireAndForget:
    """Webhook must ack 200 before the handler chain completes.

    Telegram retries any webhook call that doesn't return 200 within ~60s.
    Long content (video transcribe → analyse) easily blows past that; the
    fix is to schedule processing as a background task and return 200
    immediately. These tests pin that behaviour.
    """

    @pytest.fixture
    def client(self, main_mod, monkeypatch):
        app_stub = MagicMock()
        # AsyncMock by default — individual tests can replace process_update
        # with something that blocks to assert non-blocking ack.
        app_stub.process_update = AsyncMock()
        monkeypatch.setattr(main_mod, "telegram_app", app_stub)
        monkeypatch.setattr(main_mod, "WEBHOOK_URL", "https://example.com")
        monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", "s3cret")
        monkeypatch.setattr(main_mod.Update, "de_json", lambda data, bot: {"ok": 1})
        return TestClient(main_mod.app), app_stub

    def test_returns_200_before_long_handler_finishes(self, client, main_mod):
        c, app_stub = client
        # Block the handler "forever" until we explicitly release it. If the
        # webhook awaited process_update, the request below would hang.
        release = asyncio.Event()

        async def blocking(_update):
            await release.wait()

        app_stub.process_update = blocking

        t0 = time.monotonic()
        r = c.post(
            "/webhook",
            json={"update_id": 99},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        # Generous bound. The actual response is sub-millisecond, but TestClient
        # + anyio overhead can add a bit. Anything well under "Telegram's 60s
        # timeout" proves the non-blocking ack contract.
        assert elapsed < 0.5, f"webhook blocked {elapsed:.2f}s on slow handler"

        # The task is alive in _webhook_tasks. Let it finish so the test
        # cleanup doesn't leak warnings about un-awaited coroutines.
        _release_and_drain(main_mod, release)

    def test_background_handler_exception_logged_not_propagated(
        self, client, main_mod, caplog
    ):
        """A crashing handler must not break the webhook for subsequent calls.
        The task is fire-and-forget; failures get logged to error_log via the
        SQLite log handler, but the route always returns 200 to Telegram."""
        c, app_stub = client

        async def boom(_update):
            raise RuntimeError("handler crashed mid-analyse")

        app_stub.process_update = boom

        import logging
        with caplog.at_level(logging.ERROR, logger="main"):
            r = c.post(
                "/webhook",
                json={"update_id": 7},
                headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
            )
            assert r.status_code == 200
            _drain_webhook_tasks(main_mod)
        # Error was logged, not raised back into the request path.
        assert any("background process_update failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_webhook_tasks(main_mod, timeout: float = 2.0) -> None:
    """Block until every task currently in `_webhook_tasks` has completed.

    Tests need this because the webhook route schedules `process_update` as a
    background task and returns immediately. Asserting on the mock right after
    the request would race the task.
    """
    end = time.monotonic() + timeout
    while main_mod._webhook_tasks and time.monotonic() < end:
        time.sleep(0.01)


def _release_and_drain(main_mod, event: asyncio.Event, timeout: float = 2.0) -> None:
    """Release a test-controlled blocking handler and wait for the task to
    drain. Has to set the event from inside the same event loop the task
    is running on — TestClient's anyio portal provides that loop."""
    async def _set():
        event.set()
    # Use the portal that TestClient set up; falls back to creating one if
    # the test happens to call this outside a request context.
    from anyio.from_thread import start_blocking_portal
    with start_blocking_portal() as portal:
        portal.call(_set)
    _drain_webhook_tasks(main_mod, timeout=timeout)
