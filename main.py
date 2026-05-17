"""
filter.fyi backend — unified entry point.

Always run via uvicorn:
    uvicorn main:app --reload --host 0.0.0.0 --port 8080

Modes (auto-detected from environment):
  - No WEBHOOK_URL   → Telegram long-polling runs as a background task alongside the web server
  - WEBHOOK_URL set  → Telegram uses webhook; register at /webhook
  - No TELEGRAM_TOKEN → web UI only (no Telegram)
"""

import logging
import os
from contextlib import asynccontextmanager
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

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

telegram_app = None
if TOKEN:
    from bot.application import build_application
    telegram_app = build_application(TOKEN)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if telegram_app:
        await telegram_app.initialize()
        if WEBHOOK_URL:
            webhook_endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            await telegram_app.bot.set_webhook(webhook_endpoint)
            logger.info("Telegram webhook set to %s", webhook_endpoint)
        else:
            # Dev / local: polling as a background task on uvicorn's event loop
            await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("Telegram polling started")
        await telegram_app.start()

    yield

    if telegram_app:
        if not WEBHOOK_URL:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)

from bot.api import router as api_router  # noqa: E402

app.include_router(api_router)

_STATIC_DIR = Path(__file__).parent / "bot" / "static"


@app.get("/")
async def serve_ui():
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    if not telegram_app or not WEBHOOK_URL:
        return Response(status_code=404)
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok"}
