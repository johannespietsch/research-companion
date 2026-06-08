import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Hosted transcription is the primary path: it transcribes hours of audio in
# seconds, which is what makes the longer per-tier video caps feasible on our
# small box. Both providers speak the OpenAI audio API, so the only difference
# is base_url + model. Preference: Groq first (whisper-large-v3-turbo is far
# cheaper — ~$0.04/h vs OpenAI whisper-1's $0.36/h — and faster), then OpenAI,
# then self-hosted faster-whisper for local dev / tests (short clips only).
_GROQ_KEY = os.getenv("GROQ_API_KEY")
_OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if _GROQ_KEY:
    _PROVIDER = "groq"
    _HOSTED_KEY = _GROQ_KEY
    _HOSTED_BASE_URL = "https://api.groq.com/openai/v1"
    _HOSTED_MODEL = "whisper-large-v3-turbo"
elif _OPENAI_KEY:
    _PROVIDER = "openai"
    _HOSTED_KEY = _OPENAI_KEY
    _HOSTED_BASE_URL = None  # default OpenAI endpoint
    _HOSTED_MODEL = "whisper-1"
else:
    _PROVIDER = "local"
    _HOSTED_KEY = None
    _HOSTED_BASE_URL = None
    _HOSTED_MODEL = None

# Both Groq and OpenAI cap audio uploads at 25 MB. We always transcode to
# 16 kHz mono Opus first — Whisper resamples to 16 kHz internally anyway, and
# Opus at a low bitrate keeps even a 2-hour show well under the limit (~15 MB)
# with no meaningful accuracy loss on speech.
_UPLOAD_TARGET_HZ = 16_000
_UPLOAD_OPUS_BITRATE = "16k"
# Hard ceiling checked before upload. At 16 kbps Opus this is ~3.4 h of audio —
# beyond the 2 h signed-in cap — so it never trips in normal use; it just turns
# a pathological case into a clean WHISPER_FAILED instead of a provider 413.
_MAX_UPLOAD_BYTES = 24 * 1024 * 1024

_model = None
_hosted_client = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading Whisper base model (first run downloads ~150 MB)...")
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    return _model


def _get_hosted_client():
    global _hosted_client
    if _hosted_client is None:
        from openai import OpenAI  # Groq exposes an OpenAI-compatible API
        _hosted_client = OpenAI(api_key=_HOSTED_KEY, base_url=_HOSTED_BASE_URL)
    return _hosted_client


def _compress_for_upload(file_path: str) -> str:
    """Transcode to 16 kHz mono Opus so even long audio fits the 25 MB cap.

    Returns a path to a new temp file on success, or the original path when
    ffmpeg is unavailable / the transcode fails (caller still tries to upload
    — short clips are usually under the limit as-is).
    """
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found — uploading original audio without compression")
        return file_path
    out_path = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False).name
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", file_path,
                "-ac", "1", "-ar", str(_UPLOAD_TARGET_HZ),
                "-c:a", "libopus", "-b:a", _UPLOAD_OPUS_BITRATE,
                out_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return out_path
    except Exception as e:
        logger.warning("Audio compression failed (%s); uploading original", e)
        Path(out_path).unlink(missing_ok=True)
        return file_path


def _transcribe_hosted(file_path: str) -> str:
    client = _get_hosted_client()
    upload_path = _compress_for_upload(file_path)
    try:
        size = os.path.getsize(upload_path)
        if size > _MAX_UPLOAD_BYTES:
            raise ValueError(
                f"compressed audio {size / 1e6:.1f} MB exceeds the "
                f"{_MAX_UPLOAD_BYTES / 1e6:.0f} MB upload limit"
            )
        with open(upload_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=_HOSTED_MODEL,
                file=f,
                response_format="text",
            )
        # response_format="text" yields a plain string; be defensive in case
        # the SDK returns an object with a `.text` attribute instead.
        text = resp if isinstance(resp, str) else getattr(resp, "text", "")
        return (text or "").strip()
    finally:
        if upload_path != file_path:
            Path(upload_path).unlink(missing_ok=True)


def _transcribe_local(file_path: str) -> str:
    model = _get_model()
    segments, info = model.transcribe(file_path, beam_size=5)
    text = " ".join(s.text for s in segments).strip()
    logger.info(f"Transcribed {file_path} ({info.language}, {info.duration:.1f}s)")
    return text


def _transcribe_sync(file_path: str) -> str:
    """Transcribe an audio/video file to text.

    Uses the hosted provider (Groq, else OpenAI) when a key is set — the only
    path that can keep up with the longer per-tier video caps. A hosted failure
    raises so the caller surfaces WHISPER_FAILED rather than silently dropping
    to the local CPU model, which can't realistically finish a long file on our
    box. Without a key we use the local model (dev / tests; short clips only).
    """
    if _PROVIDER != "local":
        return _transcribe_hosted(file_path)
    return _transcribe_local(file_path)


async def transcribe(file_path: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, file_path)
