"""Tests for bot/transcriber.py — the hosted-vs-local routing.

Network is never hit: we mock the hosted client and the local model and assert
which one `_transcribe_sync` picks based on the resolved provider (Groq /
OpenAI when a key is set, else local faster-whisper).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot import transcriber


def test_routes_to_hosted_when_provider_set():
    with patch.object(transcriber, "_PROVIDER", "groq"), \
         patch.object(transcriber, "_transcribe_hosted", return_value="hosted text") as hosted, \
         patch.object(transcriber, "_transcribe_local") as local:
        out = transcriber._transcribe_sync("/tmp/audio.mp3")

    assert out == "hosted text"
    hosted.assert_called_once_with("/tmp/audio.mp3")
    local.assert_not_called()


def test_hosted_failure_propagates_not_silently_local():
    # A long file can't realistically finish on the local CPU model, so a
    # hosted failure must raise (→ caller surfaces WHISPER_FAILED), never fall
    # back.
    with patch.object(transcriber, "_PROVIDER", "openai"), \
         patch.object(transcriber, "_transcribe_hosted", side_effect=RuntimeError("boom")), \
         patch.object(transcriber, "_transcribe_local") as local:
        try:
            transcriber._transcribe_sync("/tmp/audio.mp3")
            assert False, "expected RuntimeError to propagate"
        except RuntimeError:
            pass
    local.assert_not_called()


def test_routes_to_local_when_no_provider():
    with patch.object(transcriber, "_PROVIDER", "local"), \
         patch.object(transcriber, "_transcribe_local", return_value="local text") as local, \
         patch.object(transcriber, "_transcribe_hosted") as hosted:
        out = transcriber._transcribe_sync("/tmp/audio.mp3")

    assert out == "local text"
    local.assert_called_once_with("/tmp/audio.mp3")
    hosted.assert_not_called()


def test_hosted_upload_compresses_and_cleans_up(tmp_path):
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"fake-audio")
    compressed = tmp_path / "small.ogg"
    compressed.write_bytes(b"opus")

    fake_client = MagicMock()
    fake_client.audio.transcriptions.create.return_value = "transcribed"

    with patch.object(transcriber, "_get_hosted_client", return_value=fake_client), \
         patch.object(transcriber, "_compress_for_upload", return_value=str(compressed)):
        out = transcriber._transcribe_hosted(str(src))

    assert out == "transcribed"
    # The compressed temp upload is removed; the original is left to its caller.
    assert not compressed.exists()
    assert src.exists()


def test_hosted_oversize_upload_is_split_and_joined(tmp_path):
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"fake-audio")
    compressed = tmp_path / "big.ogg"
    compressed.write_bytes(b"x" * (transcriber._MAX_UPLOAD_BYTES + 1))
    segs = []
    for i in range(3):
        seg = tmp_path / f"seg_{i}.ogg"
        seg.write_bytes(b"s")
        segs.append(str(seg))

    with patch.object(transcriber, "_compress_for_upload", return_value=str(compressed)), \
         patch.object(transcriber, "_split_audio", return_value=segs), \
         patch.object(transcriber, "_upload_once", side_effect=["one", "two", "three"]) as up:
        out = transcriber._transcribe_hosted(str(src))

    assert out == "one\n\ntwo\n\nthree"
    assert up.call_count == 3
    # The temp upload is still cleaned up; the caller's original is untouched.
    assert not compressed.exists()
    assert src.exists()


def test_segment_upload_retries_then_succeeds():
    with patch.object(transcriber, "_upload_once", side_effect=[RuntimeError("429"), "ok"]), \
         patch.object(transcriber.time, "sleep") as sleep:
        assert transcriber._upload_segment("/tmp/x.ogg") == "ok"
    sleep.assert_called_once()


def test_segment_upload_gives_up_after_attempts():
    with patch.object(transcriber, "_upload_once", side_effect=RuntimeError("down")) as up, \
         patch.object(transcriber.time, "sleep"):
        try:
            transcriber._upload_segment("/tmp/x.ogg")
            assert False, "expected failure to propagate"
        except RuntimeError:
            pass
    assert up.call_count == transcriber._SEGMENT_ATTEMPTS


def test_uncompressed_oversize_audio_is_rejected(tmp_path):
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"x" * (transcriber._MAX_UPLOAD_BYTES + 1))
    with patch.object(transcriber, "_compress_for_upload", return_value=str(src)), \
         patch.object(transcriber, "_upload_once") as up:
        try:
            transcriber._transcribe_hosted(str(src))
            assert False, "expected ValueError"
        except ValueError:
            pass
    up.assert_not_called()
    assert src.exists()
