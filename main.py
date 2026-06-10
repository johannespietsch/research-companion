"""
filter.fyi backend — unified entry point.

Always run via uvicorn:
    uvicorn main:app --reload --host 0.0.0.0 --port 8080

Modes (auto-detected from environment):
  - No WEBHOOK_URL   → Telegram long-polling runs as a background task alongside the web server
  - WEBHOOK_URL set  → Telegram uses webhook; register at /webhook
  - No TELEGRAM_TOKEN → web UI only (no Telegram)
"""

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from telegram import Update

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Capture WARNING+ records into the SQLite error_log table so scripts/scan_errors.py
# can scan them daily and file GH issues for unhandled bugs. See bot/error_logging.py.
from bot.error_logging import install as install_sqlite_log_handler  # noqa: E402

install_sqlite_log_handler()

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# Shared secret echoed by Telegram in the X-Telegram-Bot-Api-Secret-Token header.
# Without it, anyone who learns the webhook URL could POST forged updates and act
# as any chat. Required whenever WEBHOOK_URL is set (see lifespan + /webhook).
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")

telegram_app = None
if TOKEN:
    from bot.application import build_application
    telegram_app = build_application(TOKEN)


# Daily maintenance loops run once a day inside the bot process because the Fly
# volume holding the SQLite DB can only be attached to one machine at a time —
# a separate scheduled machine couldn't open the same database.
from bot.scheduling import next_daily_run  # noqa: E402

# Error scan: disabled when SCAN_ERRORS_ENABLED is unset/false so local dev
# doesn't file GH issues by accident.
_SCAN_ERRORS_ENABLED = os.getenv("SCAN_ERRORS_ENABLED", "").lower() in ("1", "true", "yes")
_SCAN_HOUR_UTC = int(os.getenv("SCAN_ERRORS_HOUR_UTC", "3"))

# Prune: bounds disk growth (old error_log, expired caches/jobs/link codes).
# Defaults ON — it's idempotent and side-effect-free (only deletes already-
# expired rows), so it should just run in prod without a manual flag. Offset an
# hour from the scan so the two don't fire together. Opt out with PRUNE_ENABLED=false.
_PRUNE_ENABLED = os.getenv("PRUNE_ENABLED", "true").lower() in ("1", "true", "yes")
_PRUNE_HOUR_UTC = int(os.getenv("PRUNE_HOUR_UTC", "4"))


async def _daily_error_scan_loop() -> None:
    """Wake once a day at SCAN_HOUR_UTC and run scripts.scan_errors.run_scan."""
    from scripts.scan_errors import run_scan

    while True:
        now = datetime.now(timezone.utc)
        next_run = next_daily_run(now, _SCAN_HOUR_UTC)
        sleep_s = (next_run - now).total_seconds()
        logger.info("Next error scan at %s UTC (%.0fs)", next_run.isoformat(), sleep_s)
        try:
            await asyncio.sleep(sleep_s)
            await asyncio.to_thread(run_scan, since_hours=24, dry_run=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily error scan failed; will retry tomorrow")


async def _daily_prune_loop() -> None:
    """Wake once a day at PRUNE_HOUR_UTC and run bot.db.prune_maintenance."""
    from bot.db import prune_maintenance

    while True:
        now = datetime.now(timezone.utc)
        next_run = next_daily_run(now, _PRUNE_HOUR_UTC)
        sleep_s = (next_run - now).total_seconds()
        logger.info("Next prune at %s UTC (%.0fs)", next_run.isoformat(), sleep_s)
        try:
            await asyncio.sleep(sleep_s)
            counts = await asyncio.to_thread(prune_maintenance)
            logger.info("prune: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily prune failed; will retry tomorrow")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if telegram_app:
        await telegram_app.initialize()
        if WEBHOOK_URL:
            if not WEBHOOK_SECRET:
                # Fail closed: registering an unauthenticated webhook would let
                # anyone forge updates. Don't set it — the bot stays silent
                # until TELEGRAM_WEBHOOK_SECRET is configured.
                logger.error(
                    "WEBHOOK_URL is set but TELEGRAM_WEBHOOK_SECRET is missing — "
                    "refusing to register an unauthenticated webhook"
                )
            else:
                webhook_endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook"
                await telegram_app.bot.set_webhook(
                    webhook_endpoint, secret_token=WEBHOOK_SECRET
                )
                logger.info("Telegram webhook set to %s", webhook_endpoint)
        else:
            # Dev / local: polling as a background task on uvicorn's event loop
            await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("Telegram polling started")
        await telegram_app.start()

    maintenance_tasks: list[asyncio.Task] = []
    if _SCAN_ERRORS_ENABLED:
        maintenance_tasks.append(asyncio.create_task(_daily_error_scan_loop()))
        logger.info("Daily error scan enabled (hour=%d UTC)", _SCAN_HOUR_UTC)
    if _PRUNE_ENABLED:
        maintenance_tasks.append(asyncio.create_task(_daily_prune_loop()))
        logger.info("Daily prune enabled (hour=%d UTC)", _PRUNE_HOUR_UTC)

    yield

    for task in maintenance_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if telegram_app:
        if not WEBHOOK_URL:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)

from bot.api import router as api_router  # noqa: E402
from bot.admin import router as admin_router  # noqa: E402

app.include_router(api_router)
app.include_router(admin_router)

_STATIC_DIR = Path(__file__).parent / "bot" / "static"


@app.get("/")
async def serve_ui():
    return FileResponse(_STATIC_DIR / "index.html")


def _webhook_secret_ok(provided: str | None) -> bool:
    """True iff the request carries the configured Telegram secret token.

    Fail closed: if no secret is configured, no request is accepted.
    """
    if not WEBHOOK_SECRET:
        return False
    return secrets.compare_digest(provided or "", WEBHOOK_SECRET)


# Live references to in-flight webhook handlers. Background tasks can be
# garbage-collected if nothing holds a reference to them — we add each task
# here on creation and let `add_done_callback` clean it up on completion.
_webhook_tasks: set[asyncio.Task] = set()


async def _process_update_in_background(update: Update) -> None:
    """Run the Telegram handler chain off the webhook request path.

    Telegram retries any webhook call that doesn't ack within ~60s. Long-form
    content (video transcribe → summarise → analyse) easily exceeds that, so
    awaiting `process_update` inside the request handler turned the same
    update into an N-times-retried storm — every retry started a fresh chain
    while the previous one was still spending tokens. Decoupling the ack from
    the work makes the timeout irrelevant. Errors are logged but not
    re-raised — the task is fire-and-forget by design.
    """
    try:
        await telegram_app.process_update(update)
    except Exception:
        update_id = getattr(update, "update_id", "?")
        logger.exception("background process_update failed for update %s", update_id)


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    if not telegram_app or not WEBHOOK_URL:
        return Response(status_code=404)
    # Reject anything that doesn't echo our secret token — this is the only
    # thing standing between the public URL and forged Telegram updates.
    if not _webhook_secret_ok(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
        logger.warning("Rejected /webhook call with missing/invalid secret token")
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    # Belt-and-braces dedup: even with the fire-and-forget fix below, an
    # earlier blocked-handler era backlog (or any future bug that re-stalls
    # the ack path) could see Telegram retrying the same update_id while a
    # background task is still running. claim_telegram_update is an atomic
    # INSERT OR IGNORE — only the first call gets True. Updates without an
    # update_id (malformed payloads / test stubs) bypass dedup, since there's
    # nothing meaningful to key on.
    if update is not None and getattr(update, "update_id", None) is not None:
        from bot.db import claim_telegram_update
        if not claim_telegram_update(update.update_id):
            logger.info("dropping duplicate webhook for update_id=%s", update.update_id)
            return Response(status_code=200)
    # Do NOT await — see _process_update_in_background. We must ack within
    # Telegram's webhook timeout (~60s) regardless of how long the handler
    # chain ends up running, or the same update will be redelivered and
    # double-processed.
    task = asyncio.create_task(_process_update_in_background(update))
    _webhook_tasks.add(task)
    task.add_done_callback(_webhook_tasks.discard)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok"}
