"""Telegram '🔧 try it' suggestion keyboard (issue #49).

Instead of dumping every suggestion's full copy-to-AI brief inline, the analysis
reply carries one button per suggestion; the brief is sent on demand when tapped.
"""
from __future__ import annotations


_ANALYSIS = {
    "verdict": "watch",
    "suggestions": [
        {"title": "Add a reranker", "detail": "…", "first_step": "…", "effort": "~2 hrs"},
        {"title": "Build an eval harness", "detail": "…", "first_step": "…", "effort": "multi-week"},
    ],
}


class TestSuggestionsKeyboard:
    def test_one_button_per_suggestion_with_callback_data(self):
        from bot.handlers import _suggestions_keyboard

        kb = _suggestions_keyboard(_ANALYSIS, item_id=42)
        rows = kb.inline_keyboard
        assert len(rows) == 2
        assert [b.callback_data for row in rows for b in row] == ["try:42:0", "try:42:1"]
        first = rows[0][0]
        assert "Add a reranker" in first.text
        assert "~2 hrs" in first.text  # effort shown on the button

    def test_none_without_item_id_or_suggestions(self):
        from bot.handlers import _suggestions_keyboard

        assert _suggestions_keyboard(_ANALYSIS, item_id=None) is None
        assert _suggestions_keyboard({"suggestions": []}, item_id=1) is None
        assert _suggestions_keyboard({}, item_id=1) is None

    def test_button_label_is_truncated(self):
        from bot.handlers import _suggestions_keyboard

        long = {"suggestions": [{"title": "x" * 200, "effort": ""}]}
        kb = _suggestions_keyboard(long, item_id=7)
        assert len(kb.inline_keyboard[0][0].text) <= 60


class TestOversizedFileGuard:
    """Telegram caps bot downloads at 20 MB; oversized audio/video must fail
    gracefully (issue #8) instead of an unhandled BadRequest."""

    def test_too_big_threshold(self):
        from bot.handlers import _too_big, _TELEGRAM_MAX_DOWNLOAD_BYTES
        assert _too_big(_TELEGRAM_MAX_DOWNLOAD_BYTES + 1) is True
        assert _too_big(_TELEGRAM_MAX_DOWNLOAD_BYTES) is False
        assert _too_big(None) is False  # unknown size → let the download try

    def test_oversized_audio_replies_and_skips_download(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from bot import handlers

        monkeypatch.setattr(handlers, "get_or_create_user_by_telegram", lambda _id: 1)
        audio = MagicMock()
        audio.file_size = 25 * 1024 * 1024
        audio.get_file = AsyncMock()
        update = MagicMock()
        update.effective_user.id = 7
        update.message.audio = audio
        update.message.reply_text = AsyncMock()

        asyncio.run(handlers.handle_audio(update, MagicMock()))

        audio.get_file.assert_not_called()  # never attempted the download
        msg = update.message.reply_text.call_args[0][0]
        assert "20 MB" in msg and "link" in msg
