import base64
import logging
import tempfile
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from bot.analyzer import analyze, analyze_image, summarize_content, to_json_str
from bot.config import MAX_CONTENT_CHARS
from bot.db import get_or_create_user_by_telegram, save_item
from bot.fetch_errors import user_message as fetch_error_message
from bot.fetcher import fetch_url
from bot.formatting import format_analysis
from bot.storage import save_file_from_path
from bot.transcriber import transcribe

logger = logging.getLogger(__name__)


async def _describe_images(image_urls: list[str]) -> str:
    """Download each image URL and return a combined description string."""
    descriptions = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for url in image_urls:
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                b64 = base64.b64encode(resp.content).decode()
                desc = analyze_image(b64)
                descriptions.append(desc)
            except Exception:
                logger.exception(f"Failed to analyse image {url}")
    if not descriptions:
        return ""
    joined = "\n\n".join(f"[Image {i+1}]: {d}" for i, d in enumerate(descriptions))
    return f"\n\nIMAGE DESCRIPTIONS:\n{joined}"


async def _analyze_and_reply(
    update: Update,
    user_id: int,
    text: str,
    source_type: str = "note",
    source: str = "",
    user_note: str = "",
    file_path: str = "",
    store_content: str | None = None,
) -> None:
    try:
        analysis = analyze(text, user_id)
    except Exception as e:
        logger.exception("Analysis failed")
        await update.message.reply_text(f"Analysis failed: {e}\n\nThe content was not saved.")
        return
    save_item(
        user_id=user_id,
        source_type=source_type,
        source=source,
        # `store_content` lets callers persist a condensed summary instead of
        # the full text (used for fetched third-party URLs); defaults to `text`
        # for the user's own notes/uploads.
        content=store_content if store_content is not None else text,
        analysis=to_json_str(analysis),
        user_note=user_note,
        file_path=file_path,
    )
    formatted = format_analysis(analysis)
    await update.message.reply_text(formatted, parse_mode="HTML")


# --- Text & URLs ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    text = message.text or ""
    if not text:
        return

    user_id = get_or_create_user_by_telegram(update.effective_user.id)
    entities = message.entities or []
    urls = [
        text[e.offset: e.offset + e.length] if e.type == "url" else e.url
        for e in entities
        if e.type in ("url", "text_link")
    ]

    if urls:
        # Extract user context (message text minus the URLs themselves)
        user_note = text
        for url_str in urls:
            user_note = user_note.replace(url_str, "")
        user_note = " ".join(user_note.split()).strip()

        for url in urls:
            await message.reply_text(f"Fetching {url} ...")
            fetched = await fetch_url(url)
            if not fetched["text"].strip():
                await message.reply_text(fetch_error_message(fetched.get("reason"), url))
                continue
            await message.reply_text("Analyzing...")
            text = fetched["text"]
            image_urls = fetched.get("image_urls") or []
            if image_urls:
                text += await _describe_images(image_urls)
            await _analyze_and_reply(
                update, user_id, text,
                source_type="url", source=url, user_note=user_note,
                file_path=fetched.get("file_path", ""),
                # Store only a condensed summary of fetched third-party content.
                store_content=summarize_content(text),
            )
    else:
        await message.reply_text("Analyzing...")
        await _analyze_and_reply(update, user_id, text, source_type="note")


# --- Voice messages ---

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_or_create_user_by_telegram(update.effective_user.id)
    await update.message.reply_text("Transcribing voice message...")
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        path = f.name
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(path)
        text = await transcribe(path)
        if not text:
            await update.message.reply_text("Could not transcribe audio.")
            return
        await update.message.reply_text(f"Transcript:\n{text[:300]}{'...' if len(text) > 300 else ''}")
        await update.message.reply_text("Analyzing...")
        stored_path = save_file_from_path(path, ".ogg")
        await _analyze_and_reply(update, user_id, text, source_type="voice_memo", file_path=stored_path)
    finally:
        Path(path).unlink(missing_ok=True)


# --- Audio files ---

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_or_create_user_by_telegram(update.effective_user.id)
    audio = update.message.audio
    suffix = f".{audio.mime_type.split('/')[-1]}" if audio.mime_type else ".mp3"
    await update.message.reply_text(f"Transcribing audio: {audio.file_name or 'file'}...")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        path = f.name
    try:
        audio_file = await audio.get_file()
        await audio_file.download_to_drive(path)
        text = await transcribe(path)
        if not text:
            await update.message.reply_text("Could not transcribe audio.")
            return
        await update.message.reply_text("Analyzing...")
        stored_path = save_file_from_path(path, suffix)
        await _analyze_and_reply(
            update, user_id, text,
            source_type="audio", source=audio.file_name or "",
            file_path=stored_path,
        )
    finally:
        Path(path).unlink(missing_ok=True)


# --- Video & video notes ---

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_or_create_user_by_telegram(update.effective_user.id)
    video = update.message.video or update.message.video_note
    await update.message.reply_text("Extracting and transcribing video audio...")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        path = f.name
    try:
        video_file = await video.get_file()
        await video_file.download_to_drive(path)
        text = await transcribe(path)
        if not text:
            await update.message.reply_text("No speech detected in video.")
            return
        await update.message.reply_text("Analyzing...")
        stored_path = save_file_from_path(path, ".mp4")
        await _analyze_and_reply(update, user_id, text, source_type="video", file_path=stored_path)
    finally:
        Path(path).unlink(missing_ok=True)


# --- Photos (vision) ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_or_create_user_by_telegram(update.effective_user.id)
    await update.message.reply_text("Analyzing image...")
    photo = update.message.photo[-1]  # largest available size
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    try:
        photo_file = await photo.get_file()
        await photo_file.download_to_drive(path)
        with open(path, "rb") as img:
            b64 = base64.b64encode(img.read()).decode()
        caption = update.message.caption or ""
        try:
            text = analyze_image(b64, caption)
        except Exception as e:
            logger.exception("Image analysis failed")
            await update.message.reply_text(f"Image analysis failed: {e}")
            return
        await update.message.reply_text("Analyzing...")
        stored_path = save_file_from_path(path, ".jpg")
        await _analyze_and_reply(
            update, user_id, text,
            source_type="photo", user_note=caption,
            file_path=stored_path,
        )
    finally:
        Path(path).unlink(missing_ok=True)


# --- Documents (PDF, text files, audio attachments) ---

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_or_create_user_by_telegram(update.effective_user.id)
    doc = update.message.document
    mime = doc.mime_type or ""
    name = doc.file_name or "document"
    suffix = f".{name.rsplit('.', 1)[-1]}" if "." in name else ".bin"

    await update.message.reply_text(f"Processing {name}...")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        path = f.name
    try:
        try:
            doc_file = await doc.get_file()
        except Exception as e:
            await update.message.reply_text(f"Could not retrieve file: {e}")
            Path(path).unlink(missing_ok=True)
            return
        await doc_file.download_to_drive(path)

        if "pdf" in mime:
            import pdfplumber  # heavy import — defer to PDF branch
            text = ""
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ""
            text = text[:MAX_CONTENT_CHARS]
        elif mime.startswith("text/"):
            with open(path, "r", errors="ignore") as fh:
                text = fh.read(MAX_CONTENT_CHARS)
        elif mime.startswith("audio/") or suffix in (".ogg", ".mp3", ".m4a", ".wav", ".flac"):
            text = await transcribe(path)
        else:
            await update.message.reply_text(f"Unsupported document type: {mime}")
            return

        if not text.strip():
            await update.message.reply_text("Could not extract text from document.")
            return

        await update.message.reply_text("Analyzing...")
        stored_path = save_file_from_path(path, suffix)
        await _analyze_and_reply(
            update, user_id, text,
            source_type="document", source=name,
            user_note=update.message.caption or "",
            file_path=stored_path,
        )
    finally:
        Path(path).unlink(missing_ok=True)
