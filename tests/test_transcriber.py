"""Tests for bot/transcriber.py — the Groq-vs-local routing.

Network is never hit: we mock the Groq client and the local model and assert
which one `_transcribe_sync` picks based on whether GROQ_API_KEY is set.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot import transcriber


def test_routes_to_groq_when_key_set():
    with patch.object(transcriber, "_GROQ_KEY", "gsk-test"), \
         patch.object(transcriber, "_transcribe_groq", return_value="groq text") as groq, \
         patch.object(transcriber, "_transcribe_local") as local:
        out = transcriber._transcribe_sync("/tmp/audio.mp3")

    assert out == "groq text"
    groq.assert_called_once_with("/tmp/audio.mp3")
    local.assert_not_called()


def test_groq_failure_propagates_not_silently_local():
    # A long file can't realistically finish on the local CPU model, so a Groq
    # failure must raise (→ caller surfaces WHISPER_FAILED), never fall back.
    with patch.object(transcriber, "_GROQ_KEY", "gsk-test"), \
         patch.object(transcriber, "_transcribe_groq", side_effect=RuntimeError("boom")), \
         patch.object(transcriber, "_transcribe_local") as local:
        try:
            transcriber._transcribe_sync("/tmp/audio.mp3")
            assert False, "expected RuntimeError to propagate"
        except RuntimeError:
            pass
    local.assert_not_called()


def test_routes_to_local_when_no_key():
    with patch.object(transcriber, "_GROQ_KEY", None), \
         patch.object(transcriber, "_transcribe_local", return_value="local text") as local, \
         patch.object(transcriber, "_transcribe_groq") as groq:
        out = transcriber._transcribe_sync("/tmp/audio.mp3")

    assert out == "local text"
    local.assert_called_once_with("/tmp/audio.mp3")
    groq.assert_not_called()


def test_groq_upload_compresses_and_cleans_up(tmp_path):
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"fake-audio")
    compressed = tmp_path / "small.ogg"
    compressed.write_bytes(b"opus")

    fake_client = MagicMock()
    fake_client.audio.transcriptions.create.return_value = "transcribed"

    with patch.object(transcriber, "_get_groq_client", return_value=fake_client), \
         patch.object(transcriber, "_compress_for_upload", return_value=str(compressed)):
        out = transcriber._transcribe_groq(str(src))

    assert out == "transcribed"
    # The compressed temp upload is removed; the original is left to its caller.
    assert not compressed.exists()
    assert src.exists()
