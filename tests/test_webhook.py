"""Tests for Telegram webhook authentication in main.py.

The /webhook route is the one public, unauthenticated-by-default surface on the
backend, so it must reject any request that doesn't echo our secret token.
"""
from __future__ import annotations

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

    def test_valid_secret_processes_update(self, client):
        c, app_stub = client
        r = c.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert r.status_code == 200
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
