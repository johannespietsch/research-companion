import asyncio
import base64
import io
import json
import logging
import os
import secrets
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse as FastAPIFileResponse
from pydantic import BaseModel

from bot.analyzer import UsageContext, analyze, analyze_image, to_json_str, to_plain_text
from bot.agent_brief import build_actions
from bot.pipeline import (
    ERR_ANALYZE_FAILED,
    ERR_BUSY,
    ERR_FETCH_FAILED,
    ERR_NO_TEXT,
    ERR_NO_TRANSCRIPT,
    PipelineError,
    analyze_url,
)
from bot.auth import require_token
from bot.concurrency import CapacityError, heavy
from bot.config import MAX_CONTENT_CHARS
from bot.fetch_errors import user_message as fetch_error_message
from bot.db import (
    FEEDBACK_SIGNALS,
    LINK_CODE_TTL_SECONDS,
    SAVED_SUGGESTION_STATUSES,
    SUGGESTION_SIGNAL_EVENTS,
    create_job,
    create_link_code,
    delete_item,
    delete_saved_suggestion,
    delete_user,
    get_all_items,
    get_item,
    get_job_record,
    get_saved_suggestions,
    get_suggestion_signals,
    get_user,
    get_user_profile,
    record_feedback,
    record_suggestion_signal,
    save_item,
    save_suggestion,
    search_items,
    set_job_done,
    set_job_error,
    set_user_profile,
    update_saved_suggestion_status,
    upsert_user_by_email,
)
from bot.storage import full_path, save_file
from bot.transcriber import transcribe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


async def _heavy_slot():
    """Holds a heavy-work slot for the request, shedding with a fast 503 when
    the box is saturated. Used by the direct-analysis endpoints that do NOT go
    through `analyze_url` (which takes its own slot) — currently `/submit/file`,
    whose transcribe+analyze work is heavy in its own right. Defined above the
    routes because Depends(...) is evaluated at import time."""
    try:
        async with heavy.slot():
            yield
    except CapacityError:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "busy",
                "message": "We're handling a burst of requests right now — try again in a few seconds.",
            },
            headers={"Retry-After": "5"},
        )


# Keeps references to running background tasks alive until they complete.
# Without this, Python's GC may collect the task before it finishes.
_background_tasks: set[asyncio.Task] = set()

# Tracks the current processing step for in-flight jobs so the poll endpoint
# can tell the browser what stage it's at. Keyed by job_id; cleaned up in the
# finally block of _run_job. Safe for the single-process Fly deployment.
_job_steps: dict[str, str] = {}

# How long a polling job waits for a heavy-work slot before giving up as busy.
# Generous (the browser is patiently polling) so transient bursts queue and
# succeed rather than failing fast like the synchronous web callers do.
_JOB_CAPACITY_WAIT_S = float(os.getenv("JOB_CAPACITY_WAIT_S", "45"))

# Preview length sent to the Worker — keeps the payload compact while still
# giving the UI something to render. The full extracted text only ever exists
# in-memory on this request.
TRY_PREVIEW_CHARS = 2000


def _actions_for(
    analysis: dict,
    *,
    user_id: int | None,
    source_text: str,
    source_title: str = "",
    source_url: str = "",
) -> list[dict]:
    """Build the agent-handoff `actions` list for a response.

    Personalizes the brief with the signed-in user's profile (anon gets none),
    and grounds it in a bounded excerpt of the content. Pure templating — no
    extra LLM calls.
    """
    profile = get_user_profile(user_id) if user_id is not None else ""
    return build_actions(
        analysis,
        profile=profile or "",
        source_title=source_title,
        source_url=source_url,
        summary_excerpt=source_text or "",
    )


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

@router.get("/items")
async def list_items(q: str | None = None, user_id: int = Depends(require_token)):
    rows = search_items(q, user_id) if q else get_all_items(user_id)
    return [dict(r) for r in rows]


@router.get("/items/{item_id}")
async def show_item(item_id: int, user_id: int = Depends(require_token)):
    row = get_item(item_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.delete("/items/{item_id}", status_code=204)
async def remove_item(item_id: int, user_id: int = Depends(require_token)):
    if not get_item(item_id, user_id):
        raise HTTPException(status_code=404, detail="Not found")
    delete_item(item_id, user_id)


@router.get("/items/{item_id}/file")
async def download_file(item_id: int, user_id: int = Depends(require_token)):
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
    user_id: int = Depends(require_token),
):
    analysis = analyze(text, ctx=UsageContext(user_id=user_id, source_type="note"))
    save_item(user_id, "note", "", text, to_json_str(analysis), user_note)
    return {
        "analysis": to_plain_text(analysis),
        "analysis_data": analysis,
        "actions": _actions_for(analysis, user_id=user_id, source_text=text),
    }


@router.post("/submit/url", status_code=201)
async def submit_url(
    url: str = Form(...),
    user_note: str = Form(""),
    user_id: int = Depends(require_token),
):
    try:
        result = await analyze_url(
            url,
            ctx=UsageContext(user_id=user_id),
            save_for_user_id=user_id,
            user_note=user_note,
        )
    except PipelineError as e:
        raise _pipeline_error_to_http(e, url)
    return {
        "analysis": to_plain_text(result.analysis),
        "analysis_data": result.analysis,
        "actions": _actions_for(
            result.analysis,
            user_id=user_id,
            source_text=result.summary,
            source_title=result.title,
            source_url=url,
        ),
    }


@router.post("/submit/file", status_code=201)
async def submit_file(
    file: UploadFile = File(...),
    user_note: str = Form(""),
    user_id: int = Depends(require_token),
    _slot: None = Depends(_heavy_slot),
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
        text = analyze_image(b64, user_note, ctx=UsageContext(user_id=user_id, source_type="photo"))
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

    analysis = analyze(text, ctx=UsageContext(user_id=user_id, source_type=source_type))
    save_item(user_id, source_type, name, text, to_json_str(analysis), user_note, file_path=stored_file_path)
    return {
        "analysis": to_plain_text(analysis),
        "analysis_data": analysis,
        "actions": _actions_for(
            analysis, user_id=user_id, source_text=text, source_title=name
        ),
    }


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@router.get("/profile")
async def get_profile_endpoint(user_id: int = Depends(require_token)):
    return {"content": get_user_profile(user_id)}


@router.put("/profile", status_code=200)
async def set_profile_endpoint(
    content: str = Form(...),
    user_id: int = Depends(require_token),
):
    set_user_profile(user_id, content)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Anonymous /api/try — called by the filter.fyi Cloudflare Worker
# ---------------------------------------------------------------------------

class TryRequest(BaseModel):
    url: str
    # Optional: the Worker's anon UUID, used purely for usage attribution so we
    # can group anon LLM spend per visitor in the admin dashboard. Server keeps
    # accepting requests without it during the rollout.
    anon_id: str | None = None


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


def _pipeline_error_to_http(e: PipelineError, url: str) -> HTTPException:
    """Translate a PipelineError into the JSON HTTPException shape every web
    URL endpoint returns — kept centralised so the Worker contract stays
    consistent across /api/try, /api/library/add, and /api/job results."""
    reason = e.fetched.get("reason")
    if e.code == ERR_BUSY:
        return HTTPException(
            status_code=503,
            detail={"error": "busy", "message": str(e)},
            headers={"Retry-After": "5"},
        )
    if e.code in (ERR_NO_TEXT, ERR_NO_TRANSCRIPT):
        return HTTPException(
            status_code=422,
            detail={
                "error": e.code,
                "reason": reason,
                "message": fetch_error_message(reason, url),
            },
        )
    if e.code == ERR_FETCH_FAILED:
        return HTTPException(status_code=502, detail={"error": "fetch-failed"})
    # ERR_ANALYZE_FAILED and any unknown future code default to 502.
    return HTTPException(status_code=502, detail={"error": e.code})


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
        result = await analyze_url(url, ctx=UsageContext(anon_id=req.anon_id))
    except PipelineError as e:
        raise _pipeline_error_to_http(e, url)

    # Worker contract: verdict at the top level, analysis dict has the other
    # five fields. analyzer.analyze() returns all six in one dict — split here.
    analysis = result.analysis
    verdict = analysis.pop("verdict", "skim")

    return {
        "url": url,
        "title": result.title or url,
        "source_type": result.source_type,
        "image_urls": result.image_urls,
        "content_preview": result.summary[:TRY_PREVIEW_CHARS],
        "verdict": verdict,
        "analysis": analysis,
        "actions": _actions_for(
            analysis,
            user_id=None,
            source_text=result.summary,
            source_title=result.title,
            source_url=url,
        ),
    }


# ---------------------------------------------------------------------------
# Worker-only endpoints (shared FILTER_FYI_TRY_SECRET, same trust model as /try)
# The Cloudflare Worker calls these after it has resolved a session/cookie to a
# canonical users.id; it then passes that id in the request. The Worker is the
# only legitimate caller.
# ---------------------------------------------------------------------------

class _UserUpsertRequest(BaseModel):
    email: str


@router.post("/users/upsert")
async def user_upsert(req: _UserUpsertRequest, _: None = Depends(_require_try_secret)):
    """Get-or-create a users row by email. Returns the canonical INTEGER id."""
    email = (req.email or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail={"error": "invalid-email"})
    return {"user_id": upsert_user_by_email(email)}


@router.get("/users/{user_id}")
async def user_get(user_id: int, _: None = Depends(_require_try_secret)):
    """Return user identifiers. `api_token` is exposed only as a presence
    boolean (`has_api_token`) — never the literal token."""
    row = get_user(user_id)
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    return {
        "id": row["id"],
        "email": row["email"],
        "telegram_chat_id": row["telegram_chat_id"],
        "has_api_token": bool(row["api_token"]),
        "profile": row["profile"],
        "created_at": row["created_at"],
    }


@router.get("/users/{user_id}/export")
async def user_export(user_id: int, _: None = Depends(_require_try_secret)):
    """Full data export for a user (GDPR portability). Worker-gated.

    Never includes `api_token`; the raw telegram_chat_id is reduced to a
    linked/not-linked boolean.
    """
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    items = get_all_items(user_id)
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "id": user["id"],
            "email": user["email"],
            "telegram_linked": user["telegram_chat_id"] is not None,
            "profile": user["profile"],
            "created_at": user["created_at"],
        },
        "items": [
            {
                "id": i["id"],
                "source_type": i["source_type"],
                "source": i["source"],
                "content": i["content"],
                "analysis": i["analysis"],
                "user_note": i["user_note"],
                "created_at": i["created_at"],
            }
            for i in items
        ],
        "saved_suggestions": [
            {
                "id": s["id"],
                "item_id": s["item_id"],
                "suggestion_index": s["suggestion_index"],
                "title": s["title"],
                "detail": s["detail"],
                "effort": s["effort"],
                "first_step": s["first_step"],
                "grounded_in": s["grounded_in"],
                "status": s["status"],
                "created_at": s["created_at"],
                "updated_at": s["updated_at"],
            }
            for s in get_saved_suggestions(user_id)
        ],
        "suggestion_signals": [
            {
                "url": r["url"],
                "event": r["event"],
                "suggestion_index": r["suggestion_index"],
                "suggestion_text": r["suggestion_text"],
                "reason": r["reason"],
                "created_at": r["created_at"],
            }
            for r in get_suggestion_signals(user_id, limit=10_000)
        ],
    }


@router.delete("/users/{user_id}", status_code=204)
async def user_delete(user_id: int, _: None = Depends(_require_try_secret)):
    """Hard-delete a user and all their data (GDPR erasure). Worker-gated.

    Idempotent: deleting an unknown user is a no-op 204 — the requested end
    state (no such user) already holds. Also unlinks any stored files.
    """
    result = delete_user(user_id)
    for rel in result["file_paths"]:
        try:
            full_path(rel).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("erasure: could not remove file %s: %s", rel, e)
    logger.info(
        "erasure: user=%s items_deleted=%s files=%s",
        user_id, result["items_deleted"], len(result["file_paths"]),
    )


# Cap on profile length — it's fed into every analysis prompt, so keep it bounded.
PROFILE_MAX_CHARS = 4_000


class _ProfileUpdateRequest(BaseModel):
    profile: str


@router.put("/users/{user_id}/profile", status_code=200)
async def user_set_profile(
    user_id: int, req: _ProfileUpdateRequest, _: None = Depends(_require_try_secret)
):
    """Set a user's personalization profile (the lens fed to the analyser).

    Worker-gated; the Worker resolves the session to user_id. 404 for an unknown
    user so we don't silently create profiles for ids that don't exist.
    """
    if not get_user(user_id):
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    profile = (req.profile or "").strip()[:PROFILE_MAX_CHARS]
    set_user_profile(user_id, profile)
    return {"profile": profile}


class _FeedbackRequest(BaseModel):
    user_id: int
    item_id: int
    signal: str


@router.post("/feedback", status_code=201)
async def feedback(req: _FeedbackRequest, _: None = Depends(_require_try_secret)):
    """Record one feedback/signal event for a user's item (personalization loop).

    Validates the signal against the allowlist and that the item belongs to the
    user (so feedback can't be written against someone else's item).
    """
    if req.signal not in FEEDBACK_SIGNALS:
        raise HTTPException(status_code=400, detail={"error": "invalid-signal"})
    if not get_item(req.item_id, req.user_id):
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    record_feedback(req.user_id, req.item_id, req.signal)


class _SuggestionSignalRequest(BaseModel):
    user_id: int
    event: str
    url: str = ""
    suggestion_index: int | None = None
    suggestion_text: str = ""
    reason: str = ""


@router.post("/suggestion-signals", status_code=201)
async def suggestion_signal(
    req: _SuggestionSignalRequest, _: None = Depends(_require_try_secret)
):
    """Record one suggestion interaction event for a signed-in user (#69).

    Forwarded by the Worker alongside its D1 write — D1 keeps the canonical
    stream (incl. anonymous traffic) for product analytics; this copy is what
    the analyzer's behaviour-signal digest reads (bot/signals.py). Signed-in
    only by design: anon events can't personalize anything.
    """
    if req.event not in SUGGESTION_SIGNAL_EVENTS:
        raise HTTPException(status_code=400, detail={"error": "invalid-event"})
    if not get_user(req.user_id):
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    record_suggestion_signal(
        req.user_id,
        req.event,
        url=req.url,
        suggestion_index=req.suggestion_index,
        suggestion_text=req.suggestion_text,
        reason=req.reason,
    )


# ---------------------------------------------------------------------------
# Shortlist (saved suggestions) — "later" parks a suggestion here; the Worker
# proxies these as /api/v1/saved-suggestions and injects the session's user_id.
# ---------------------------------------------------------------------------

class _SaveSuggestionRequest(BaseModel):
    user_id: int
    item_id: int
    suggestion_index: int
    title: str = ""
    detail: str = ""
    effort: str = ""
    first_step: str = ""
    grounded_in: str = ""


class _SuggestionStatusRequest(BaseModel):
    user_id: int
    status: str


@router.post("/saved-suggestions", status_code=201)
async def saved_suggestion_create(
    req: _SaveSuggestionRequest, _: None = Depends(_require_try_secret)
):
    """Park ("later") a suggestion on the user's Shortlist. Idempotent on
    (user, item, suggestion_index). Ownership-scoped: the item must belong to
    the user (404 otherwise), so you can't pin against someone else's read."""
    if not get_item(req.item_id, req.user_id):
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    saved_id = save_suggestion(
        req.user_id,
        req.item_id,
        req.suggestion_index,
        title=req.title,
        detail=req.detail,
        effort=req.effort,
        first_step=req.first_step,
        grounded_in=req.grounded_in,
    )
    return {"id": saved_id, "status": "saved"}


@router.get("/saved-suggestions")
async def saved_suggestions_list(user_id: int, _: None = Depends(_require_try_secret)):
    """The user's Shortlist, newest first. Each row carries its source URL and
    the source item's title (for the back-link) alongside the snapshot."""
    out = []
    for r in get_saved_suggestions(user_id):
        _, item_title = _extract_verdict_and_title(r["item_analysis"])
        out.append({
            "id": r["id"],
            "item_id": r["item_id"],
            "suggestion_index": r["suggestion_index"],
            "title": r["title"],
            "detail": r["detail"],
            "effort": r["effort"],
            "first_step": r["first_step"],
            "grounded_in": r["grounded_in"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "source": r["source"],
            "item_title": item_title,
        })
    return out


@router.patch("/saved-suggestions/{saved_id}")
async def saved_suggestion_update(
    saved_id: int, req: _SuggestionStatusRequest, _: None = Depends(_require_try_secret)
):
    """Advance a shortlisted suggestion's status (saved → tried → done)."""
    if req.status not in SAVED_SUGGESTION_STATUSES:
        raise HTTPException(status_code=400, detail={"error": "invalid-status"})
    if not update_saved_suggestion_status(saved_id, req.user_id, req.status):
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    return {"id": saved_id, "status": req.status}


@router.delete("/saved-suggestions/{saved_id}", status_code=204)
async def saved_suggestion_delete(
    saved_id: int, user_id: int, _: None = Depends(_require_try_secret)
):
    """Remove a suggestion from the Shortlist. Ownership-scoped (404 for
    someone else's id, mirroring the library delete)."""
    if not delete_saved_suggestion(saved_id, user_id):
        raise HTTPException(status_code=404, detail={"error": "not-found"})


class _LibraryAddRequest(BaseModel):
    user_id: int
    url: str
    user_note: str = ""


@router.post("/library/add", status_code=201)
async def library_add(req: _LibraryAddRequest, _: None = Depends(_require_try_secret)):
    """Signed-in `/api/try`: analyse a URL for the given user AND save to items.

    Mirrors `/api/try`'s response shape (so the Worker can use the same renderer)
    but adds `id`: the newly created items row id, so the Worker can deep-link
    to the entry afterwards.
    """
    url = (req.url or "").strip()
    if not _is_http_url(url):
        raise HTTPException(status_code=400, detail={"error": "invalid-url"})

    try:
        result = await analyze_url(
            url,
            ctx=UsageContext(user_id=req.user_id),
            save_for_user_id=req.user_id,
            user_note=req.user_note,
        )
    except PipelineError as e:
        raise _pipeline_error_to_http(e, url)

    analysis = result.analysis
    verdict = analysis.pop("verdict", "skim")
    return {
        "id": result.saved_id,
        "url": url,
        "title": result.title or url,
        "source_type": result.source_type,
        "image_urls": result.image_urls,
        "content_preview": result.summary[:TRY_PREVIEW_CHARS],
        "verdict": verdict,
        "analysis": analysis,
        "actions": _actions_for(
            analysis,
            user_id=req.user_id,
            source_text=result.summary,
            source_title=result.title,
            source_url=url,
        ),
    }


def _extract_verdict_and_title(analysis_json: str) -> tuple[str, str]:
    """Pull verdict + a short title-ish from a stored analysis JSON, defensively."""
    if not analysis_json:
        return "", ""
    try:
        data = json.loads(analysis_json)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    verdict = str(data.get("verdict") or "").strip().lower()
    title = str(data.get("main_idea") or "").strip()
    return verdict, title[:120]


@router.get("/library")
async def library_list(user_id: int, _: None = Depends(_require_try_secret)):
    """Lean library list for a user — enough to render `/me` without per-row fetches."""
    rows = get_all_items(user_id)
    out = []
    for r in rows:
        verdict, title = _extract_verdict_and_title(r["analysis"])
        out.append({
            "id": r["id"],
            "source_type": r["source_type"],
            "source": r["source"],
            "verdict": verdict,
            "title": title,
            "created_at": r["created_at"],
        })
    return out


@router.get("/library/{item_id}")
async def library_show(item_id: int, user_id: int, _: None = Depends(_require_try_secret)):
    """Full item — for the `/me` show page."""
    row = get_item(item_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    return dict(row)


@router.delete("/library/{item_id}", status_code=204)
async def library_delete(item_id: int, user_id: int, _: None = Depends(_require_try_secret)):
    """Delete one of the user's saved items — backs the `/me` delete action.

    Ownership-scoped: a 404 (rather than 403) for someone else's id avoids
    leaking which ids exist for other users.
    """
    if not get_item(item_id, user_id):
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    delete_item(item_id, user_id)


class _JobRequest(BaseModel):
    url: str
    user_id: int | None = None
    user_note: str = ""
    # Optional anon UUID from the Worker (anon /api/job traffic). Same purpose
    # as TryRequest.anon_id: per-visitor usage attribution only.
    anon_id: str | None = None


@router.post("/job", status_code=202)
async def start_job(req: _JobRequest, _: None = Depends(_require_try_secret)):
    """Start an async analysis job and return a job_id immediately.

    The Worker calls this instead of /api/try or /api/library/add. The browser
    polls GET /api/job/:id until status → 'done' or 'error'. The background
    task runs fetch → summarize → analyze(summary) in sequence; for signed-in
    users it also saves the item to the backend DB.
    """
    url = (req.url or "").strip()
    if not _is_http_url(url):
        raise HTTPException(status_code=400, detail={"error": "invalid-url"})

    job_id = str(uuid.uuid4())
    create_job(job_id)

    task = asyncio.create_task(_run_job(job_id, url, req.user_id, req.user_note, req.anon_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"job_id": job_id}


async def _run_job(
    job_id: str,
    url: str,
    user_id: int | None,
    user_note: str,
    anon_id: str | None = None,
) -> None:
    """Background task: routes through the unified URL pipeline and stamps
    progress steps for the polling UI. The pipeline does its own progression
    (fetch → images → summary → analyze); we surface coarse steps here so
    the browser has something to show — finer per-step progress would need
    a callback API on the pipeline, parked as a follow-up."""
    try:
        ctx = UsageContext(user_id=user_id, anon_id=anon_id, job_id=job_id)

        def _on_step(label: str) -> None:
            _job_steps[job_id] = label

        try:
            result = await analyze_url(
                url,
                ctx=ctx,
                save_for_user_id=user_id,
                user_note=user_note,
                on_step=_on_step,
                # The browser is polling, not holding a 25s request — so queue
                # for a slot under load instead of shedding immediately. Only
                # errors as busy if the backlog is still draining after this.
                capacity_timeout=_JOB_CAPACITY_WAIT_S,
            )
        except PipelineError as e:
            set_job_error(job_id, e.code, fetch_error_message(e.fetched.get("reason"), url))
            return

        analysis = result.analysis
        verdict = analysis.pop("verdict", "skim")
        # `content` is the full stored brief, exposed so the result page can
        # show the basis for the verdict. `content_preview` is kept for the
        # Worker's D1 claim path, which stays on a 2k slice to bound row size.
        payload: dict = {
            "url": url,
            "title": result.title or url,
            "source_type": result.source_type,
            "image_urls": result.image_urls,
            "content": result.summary,
            "content_preview": result.summary[:TRY_PREVIEW_CHARS],
            "verdict": verdict,
            "analysis": analysis,
            "actions": _actions_for(
                analysis,
                user_id=user_id,
                source_text=result.summary,
                source_title=result.title,
                source_url=url,
            ),
        }
        if result.saved_id is not None:
            payload["id"] = result.saved_id

        set_job_done(job_id, payload)
    except Exception:
        logger.exception("job %s: unexpected failure", job_id)
        set_job_error(job_id, "internal-error")
    finally:
        _job_steps.pop(job_id, None)


@router.get("/job/{job_id}")
async def get_job_status(job_id: str, _: None = Depends(_require_try_secret)):
    """Poll for an async job's result."""
    job = get_job_record(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not-found"})
    if job["status"] == "done":
        try:
            result = json.loads(job["result"])
        except Exception:
            result = None
        return {"status": "done", "result": result}
    if job["status"] == "error":
        return {"status": "error", "error": job["error"], "message": job["message"]}
    return {"status": "pending", "step": _job_steps.get(job_id, "fetching")}


class _ClaimRow(BaseModel):
    url: str
    title: str | None = None
    source_type: str = "article"
    content_preview: str | None = None
    verdict: str = "skim"
    analysis: dict | None = None


class _ClaimRequest(BaseModel):
    user_id: int
    rows: list[_ClaimRow]


@router.post("/claim", status_code=201)
async def claim(req: _ClaimRequest, _: None = Depends(_require_try_secret)):
    """Worker pushes a batch of anon rows here right after magic-link verify.

    The Worker reads its D1 `summaries` rows by `anon_id`, posts them here, then
    deletes them from D1. We can only store what the anon row had — full source
    text was never persisted, so `content_preview` is the best we get.
    """
    saved = 0
    for r in req.rows:
        analysis_obj = dict(r.analysis or {})
        analysis_obj["verdict"] = r.verdict or analysis_obj.get("verdict") or "skim"
        save_item(
            user_id=req.user_id,
            source_type=r.source_type,
            source=r.url,
            content=r.content_preview or "",
            analysis=to_json_str(analysis_obj),
        )
        saved += 1
    return {"count": saved}


class _LinkStartRequest(BaseModel):
    user_id: int


@router.post("/link/start", status_code=201)
async def link_start(req: _LinkStartRequest, _: None = Depends(_require_try_secret)):
    """Issue a 6-digit code the user types into the Telegram bot as `/link <code>`."""
    code = create_link_code(req.user_id)
    return {"code": code, "expires_in_seconds": LINK_CODE_TTL_SECONDS}
