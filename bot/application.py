import os
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, TypeHandler, filters

from bot.commands import cmd_start, cmd_delete, cmd_list, cmd_profile, cmd_search, cmd_show, cmd_token
from bot.handlers import (
    handle_audio,
    handle_document,
    handle_photo,
    handle_text,
    handle_video,
    handle_voice,
)

logger = logging.getLogger(__name__)

# Comma-separated Telegram user IDs that may use the bot.
# Leave unset or empty to allow everyone (useful for local dev / single-user).
_ALLOWED_IDS: set[str] = {
    uid.strip()
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


async def _check_allowlist(update: Update, context) -> None:
    """Reject updates from users not in the allowlist (when one is configured)."""
    if not _ALLOWED_IDS:
        return  # no allowlist configured — open access
    user = update.effective_user
    if user and str(user.id) not in _ALLOWED_IDS:
        logger.warning("Blocked user %s (%s)", user.id, user.username)
        if update.message:
            await update.message.reply_text("Sorry, you are not authorised to use this bot.")
        raise ApplicationHandlerStop()


# Import here to avoid circular; only needed for the stop sentinel
from telegram.ext import ApplicationHandlerStop  # noqa: E402


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    # Allowlist check runs first for every update (group -1)
    app.add_handler(TypeHandler(Update, _check_allowlist), group=-1)

    # Onboarding
    app.add_handler(CommandHandler("start", cmd_start))

    # Web UI token
    app.add_handler(CommandHandler("token", cmd_token))

    # KB commands
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("profile", cmd_profile))

    # Content ingestion
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return app
