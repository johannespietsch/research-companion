import base64
import logging
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.agent_brief import build_actions
from bot.analyzer import UsageContext, analyze, analyze_image, parse_stored, to_json_str
from bot.config import MAX_CONTENT_CHARS
from bot.db import get_item, get_or_create_user_by_telegram, get_user_profile, save_item
from bot.fetch_errors import user_message as fetch_error_message
from bot.formatting import format_agent_brief, format_analysis
from bot.pipeline import PipelineError, analyze_url
from bot.storage import save_file_from_path
from bot.transcriber import transcribe

logger = logging.getLogger(__name__)

# Telegram's Bot API caps file downloads (getFile) at 20 MB — a hard server-side
# limit we can't raise. Larger uploads fail with "File is too big"; we catch
# that early and point the user at a link instead (which the URL path now
# transcribes — see fetcher._transcribe_audio_url, #8).
_TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_TOO_BIG_MSG = (
    "That file is over Telegram's 20 MB limit for bots, so I can't download it. "
    "Paste a link to the audio or video instead and I'll fetch it directly."
)


def _too_big(file_size: int | None) -> bool:
    return bool(file_size) and file_size > _TELEGRAM_MAX_DOWNLOAD_BYTES


def _suggestions_keyboard(analysis: dict, item_id: int | None) -> InlineKeyboardMarkup | None:
    """One '🔧 <title>' button per suggestion. Tapping sends that suggestion's
    full copy-to-AI brief on demand — keeps the analysis reply short instead of
    dumping every brief inline (issue #49). Needs a saved item_id to reference."""
    suggestions = analysis.get("suggestions") or []
    if not item_id or not suggestions:
        return None
    rows = []
    for i, s in enumerate(suggestions):
        title = (s.get("title") or f"Suggestion {i + 1}").strip()
        effort = (s.get("effort") or "").strip()
        label = f"🔧 {title}" + (f" · {effort}" if effort else "")
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"try:{item_id}:{i}")])
    return InlineKeyboardMarkup(rows)


async def _reply_analysis(message, analysis: dict, item_id: int | None) -> None:
    """Send the analysis with a 'try it' button per suggestion (brief on demand)."""
    await message.reply_text(
        format_analysis(analysis),
        parse_mode="HTML",
        reply_markup=_suggestions_keyboard(analysis, item_id),
    )


async def on_try_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a 'try it' button tap: rebuild and send the one suggestion's brief."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    try:
        _, item_id_s, index_s = (query.data or "").split(":")
        item_id, index = int(item_id_s), int(index_s)
    except ValueError:
        return
    user_id = get_or_create_user_by_telegram(update.effective_user.id)
    row = get_item(item_id, user_id)
    if not row:
        await query.answer("That suggestion isn't available anymore.", show_alert=True)
        return
    analysis = parse_stored(row["analysis"]) or {}
    actions = build_actions(
        analysis,
        profile=get_user_profile(user_id) or "",
        source_url=row["source"] or "",
        summary_excerpt=row["content"] or "",
    )
    if not (0 <= index < len(actions)):
        return
    block = format_agent_brief(actions[index])
    if block:
        await query.message.reply_text(block, parse_mode="HTML")


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
    item_id = save_item(
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
    await _reply_analysis(update.message, analysis, item_id)


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
            await message.reply_text(f"Fetching and analysing {url} ...")
            try:
                result = await analyze_url(
                    url,
                    ctx=UsageContext(user_id=user_id),
                    save_for_user_id=user_id,
                    user_note=user_note,
                )
            except PipelineError as e:
                await message.reply_text(fetch_error_message(e.fetched.get("reason"), url))
                continue
            await _reply_analysis(message, result.analysis, result.saved_id)
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
    if _too_big(audio.file_size):
        await update.message.reply_text(_TOO_BIG_MSG)
        return
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
    if _too_big(video.file_size):
        await update.message.reply_text(_TOO_BIG_MSG)
        return
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
