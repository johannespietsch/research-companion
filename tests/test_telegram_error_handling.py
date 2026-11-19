"""Telegram handlers must never leave the chat silent after the "Fetching…" ack."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import handlers
from bot.pipeline import ERR_BUSY, PipelineError


def _update(text="https://example.com/a"):
    msg = MagicMock()
    msg.text = text
    msg.entities = [MagicMock(type="url", offset=0, length=len(text), url=None)]
    msg.reply_text = AsyncMock()
    upd = MagicMock()
    upd.message = msg
    upd.effective_message = msg
    upd.effective_user.id = 1
    return upd, msg


@pytest.fixture(autouse=True)
def _user(monkeypatch):
    monkeypatch.setattr(handlers, "get_or_create_user_by_telegram", lambda _id: 1)


def _replies(msg):
    return [c.args[0] for c in msg.reply_text.await_args_list]


def test_unexpected_exception_replies(monkeypatch):
    monkeypatch.setattr(handlers, "analyze_url", AsyncMock(side_effect=RuntimeError("boom")))
    upd, msg = _update()
    asyncio.run(handlers.handle_text(upd, MagicMock()))
    assert "Something went wrong" in _replies(msg)[-1]


def test_timeout_replies(monkeypatch):
    async def hang(*a, **k):
        await asyncio.sleep(10)

    monkeypatch.setattr(handlers, "analyze_url", hang)
    monkeypatch.setattr(handlers, "URL_ANALYSIS_TIMEOUT_S", 0.05)
    upd, msg = _update()
    asyncio.run(handlers.handle_text(upd, MagicMock()))
    assert "took too long" in _replies(msg)[-1]


def test_busy_replies_with_message(monkeypatch):
    err = PipelineError(ERR_BUSY, message="Service is busy; please try again in a few seconds.")
    monkeypatch.setattr(handlers, "analyze_url", AsyncMock(side_effect=err))
    upd, msg = _update()
    asyncio.run(handlers.handle_text(upd, MagicMock()))
    assert "busy" in _replies(msg)[-1]


def test_reply_analysis_falls_back_to_plain_text(monkeypatch):
    monkeypatch.setattr(handlers, "format_analysis", lambda a: "<b>Hi</b> &amp; " + "x" * 5000)
    calls = []

    async def reply(text, parse_mode=None, reply_markup=None):
        calls.append((text, parse_mode))
        if parse_mode == "HTML":
            raise RuntimeError("can't parse entities")

    msg = MagicMock()
    msg.reply_text = reply
    asyncio.run(handlers._reply_analysis(msg, {}, None))
    plain = calls[1:]
    assert len(plain) == 2 and all(p is None and len(t) <= 4096 for t, p in plain)
    assert plain[0][0].startswith("Hi & ")


def test_error_handler_notifies_user():
    from bot.application import _on_error

    upd, msg = _update()
    ctx = MagicMock()
    ctx.error = RuntimeError("x")
    asyncio.run(_on_error(upd, ctx))
    msg.reply_text.assert_awaited_once()
