import base64
import io
import logging
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse as FastAPIFileResponse
from pydantic import BaseModel

from bot.analyzer import analyze, analyze_image, to_json_str, to_plain_text
from bot.auth import require_token
from bot.config import MAX_CONTENT_CHARS
from bot.db import (
    delete_item,
    get_all_items,
    get_item,
    get_profile,
    save_item,
    search_items,
    set_profile,
)
from bot.fetcher import fetch_url
from bot.storage import full_path, save_file
from bot.transcriber import transcribe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Preview length sent to the Worker — keeps the payload compact while still
# giving the UI something to render. The full extracted text only ever exists
# in-memory on this request.
TRY_PREVIEW_CHARS = 2000

# Source types where empty extracted text means "no transcript available"
# rather than a generic extraction failure. Worker maps this to a friendly
# "video transcripts coming with the full product" notice.
_VIDEO_SOURCE_TYPES = {"youtube", "video"}


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

@router.get("/items")
async def list_items(q: str | None = None, user_id: str = Depends(require_token)):
    rows = search_items(q, user_id) if q else get_all_items(user_id)
    return [dict(r) for r in rows]


@router.get("/items/{item_id}")
async def show_item(item_id: int, user_id: str = Depends(require_token)):
    row = get_item(item_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.delete("/items/{item_id}", status_code=204)
async def remove_item(item_id: int, user_id: str = Depends(require_token)):
    if not get_item(item_id, user_id):
        raise HTTPException(status_code=404, detail="Not found")
    delete_item(item_id, user_id)


@router.get("/items/{item_id}/file")
async def download_file(item_id: int, user_id: str = Depends(require_token)):
    row = get_item(item_id, user_id)
    if not row or not row["file_path"]:
        raise HTTPException(status_code=404, detail="No file stored for this item")
    p = full_path(row["file_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FastAPIFileResponse(str(p))


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

@router.post("/submit/text", status_code=201)
async def submit_text(
    text: str = Form(...),
    user_note: str = Form(""),
    user_id: str = Depends(require_token),
):
    analysis = analyze(text, user_id)
    save_item(user_id, "note", "", text, to_json_str(analysis), user_note)
    return {"analysis": to_plain_text(analysis), "analysis_data": analysis}


@router.post("/submit/url", status_code=201)
async def submit_url(
    url: str = Form(...),
    user_note: str = Form(""),
    user_id: str = Depends(require_token),
):
    fetched = await fetch_url(url)
    if not fetched["text"].strip():
        raise HTTPException(status_code=422, detail="Could not extract content from URL")
    analysis = analyze(fetched["text"], user_id)
    save_item(user_id, "url", url, fetched["text"], to_json_str(analysis), user_note)
    return {"analysis": to_plain_text(analysis), "analysis_data": analysis}


@router.post("/submit/file", status_code=201)
async def submit_file(
    file: UploadFile = File(...),
    user_note: str = Form(""),
    user_id: str = Depends(require_token),
):
    mime = file.content_type or ""
    name = file.filename or "file"
    suffix = f".{name.rsplit('.', 1)[-1]}" if "." in name else ".bin"
    data = await file.read()

    stored_file_path = ""
    if "pdf" in mime:
        import pdfplumber  # heavy import — defer to PDF branch
        stored_file_path = save_file(data, suffix)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "".join(p.extract_text() or "" for p in pdf.pages)[:MAX_CONTENT_CHARS]
        source_type = "document"
    elif mime.startswith("text/"):
        stored_file_path = save_file(data, suffix)
        text = data.decode("utf-8", errors="ignore")[:MAX_CONTENT_CHARS]
        source_type = "document"
    elif mime.startswith("image/"):
        stored_file_path = save_file(data, suffix)
        b64 = base64.b64encode(data).decode()
        text = analyze_image(b64, user_note)
        source_type = "photo"
    elif mime.startswith("audio/") or suffix in (".ogg", ".mp3", ".m4a", ".wav", ".flac"):
        stored_file_path = save_file(data, suffix)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp_path = f.name
        try:
            text = await transcribe(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        source_type = "audio"
    else:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {mime}")

    if not text or not text.strip():
        raise HTTPException(status_code=422, detail="Could not extract text from file")

    analysis = analyze(text, user_id)
    save_item(user_id, source_type, name, text, to_json_str(analysis), user_note, file_path=stored_file_path)
    return {"analysis": to_plain_text(analysis), "analysis_data": analysis}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@router.get("/profile")
async def get_profile_endpoint(user_id: str = Depends(require_token)):
    return {"content": get_profile(user_id)}


@router.put("/profile", status_code=200)
async def set_profile_endpoint(
    content: str = Form(...),
    user_id: str = Depends(require_token),
):
    set_profile(user_id, content)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Anonymous /api/try — called by the filter.fyi Cloudflare Worker
# ---------------------------------------------------------------------------

class TryRequest(BaseModel):
    url: str


def _require_try_secret(x_filter_fyi_secret: str | None = Header(default=None)) -> None:
    expected = os.getenv("FILTER_FYI_TRY_SECRET")
    if not expected:
        # Misconfigured server — surface clearly rather than accept anything.
        raise HTTPException(status_code=503, detail={"error": "service-unavailable"})
    if not x_filter_fyi_secret or not secrets.compare_digest(x_filter_fyi_secret, expected):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def _is_http_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


@router.post("/try")
async def try_url(req: TryRequest, _: None = Depends(_require_try_secret)):
    """One-shot analysis for anonymous web users.

    Authenticated via a shared `x-filter-fyi-secret` header (the Worker is the
    only legitimate caller). Does NOT persist — anonymous rows live in the
    Worker's D1 store, keyed by `anon_id` for later claim-on-signup.
    """
    url = (req.url or "").strip()
    if not _is_http_url(url):
        raise HTTPException(status_code=400, detail={"error": "invalid-url"})

    try:
        fetched = await fetch_url(url)
    except Exception as e:
        logger.exception("fetch_url crashed for %s: %s", url, e)
        raise HTTPException(status_code=502, detail={"error": "fetch-failed"})

    text = (fetched.get("text") or "").strip()
    source_type = fetched.get("source_type") or "article"

    if not text:
        # Distinguish video-with-no-transcript from generic extraction failure
        # so the Worker can show the right message.
        if source_type in _VIDEO_SOURCE_TYPES:
            raise HTTPException(status_code=422, detail={"error": "no-transcript"})
        raise HTTPException(status_code=422, detail={"error": "extraction-failed"})

    try:
        analysis = analyze(text, user_id="")
    except Exception as e:
        logger.exception("analyze crashed for %s: %s", url, e)
        raise HTTPException(status_code=502, detail={"error": "analyze-failed"})

    # Worker contract: verdict at the top level, analysis dict has the other
    # five fields. analyzer.analyze() returns all six in one dict — split here.
    verdict = analysis.pop("verdict", "skim")

    return {
        "url": url,
        "title": fetched.get("title") or url,
        "source_type": source_type,
        "image_urls": fetched.get("image_urls") or [],
        "content_preview": text[:TRY_PREVIEW_CHARS],
        "verdict": verdict,
        "analysis": analysis,
    }
