import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Hosted transcription (Groq's whisper-large-v3-turbo) is the primary path:
# it transcribes hours of audio in seconds, which is what makes the longer
# per-tier video caps feasible on our small box. When GROQ_API_KEY is unset
# (local dev / tests) we fall back to self-hosted faster-whisper, which is
# fine for the short clips that path still handles.
_GROQ_KEY = os.getenv("GROQ_API_KEY")
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_GROQ_MODEL = "whisper-large-v3-turbo"

# Groq caps upload size (25 MB free tier). We always transcode to 16 kHz mono
# Opus first — Whisper resamples to 16 kHz internally anyway, and Opus at a low
# bitrate keeps even a 2-hour show well under the limit (~15 MB) with no
# meaningful accuracy loss on speech.
_UPLOAD_TARGET_HZ = 16_000
_UPLOAD_OPUS_BITRATE = "16k"

_model = None
_groq_client = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading Whisper base model (first run downloads ~150 MB)...")
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    return _model


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from openai import OpenAI  # Groq exposes an OpenAI-compatible API
        _groq_client = OpenAI(api_key=_GROQ_KEY, base_url=_GROQ_BASE_URL)
    return _groq_client


def _compress_for_upload(file_path: str) -> str:
    """Transcode to 16 kHz mono Opus so even long audio fits Groq's size cap.

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


def _transcribe_groq(file_path: str) -> str:
    client = _get_groq_client()
    upload_path = _compress_for_upload(file_path)
    try:
        with open(upload_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=_GROQ_MODEL,
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

    Uses Groq when GROQ_API_KEY is set (the only path that can keep up with
    the longer per-tier video caps); a Groq failure raises so the caller
    surfaces WHISPER_FAILED rather than silently dropping to the local CPU
    model, which can't realistically finish a long file on our box. Without a
    key we use the local model (dev / tests; short clips only).
    """
    if _GROQ_KEY:
        return _transcribe_groq(file_path)
    return _transcribe_local(file_path)


async def transcribe(file_path: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, file_path)
