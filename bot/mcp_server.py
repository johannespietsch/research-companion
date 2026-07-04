"""MCP server — filter.fyi as a first-class tool for AI agents.

Mounted at `/mcp` (Streamable HTTP, stateless). Lets Claude Code / Claude
Desktop / Cursor / any MCP client analyse links, search the user's library,
and read/tune their lens — so the product works where the user's agent
already lives, not just in a browser tab.

Auth: the same per-user Bearer token as the REST API (`users.api_token`,
minted via Telegram `/token`). A tiny ASGI middleware validates the header
and stashes the canonical users.id in a ContextVar the tools read; there is
no anonymous MCP access.

Client config (e.g. Claude Code):

    claude mcp add --transport http filter-fyi https://<backend>/mcp \
        --header "Authorization: Bearer <token>"
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar

from mcp.server.fastmcp import FastMCP

from bot.analyzer import UsageContext
from bot.db import get_all_items, get_item, get_user_by_token, get_user_profile, search_items, set_user_field
from bot.pipeline import PipelineError, analyze_text, analyze_url

logger = logging.getLogger(__name__)

# The authenticated caller for the current request. Set by the auth middleware
# before the MCP app sees the request; tools must only read it via
# `_require_user_id()`.
_current_user_id: ContextVar[int | None] = ContextVar("mcp_user_id", default=None)


def _require_user_id() -> int:
    uid = _current_user_id.get()
    if uid is None:  # middleware rejects unauthenticated requests; belt & braces
        raise RuntimeError("MCP tool invoked without an authenticated user")
    return uid


mcp = FastMCP(
    name="filter-fyi",
    instructions=(
        "filter.fyi turns anything the user reads/watches/listens to into a "
        "verdict (worth the time / worth a skim / skip-able) plus concrete "
        "next-step suggestions. Use `analyze` on URLs the user shares, "
        "`search_library` / `get_library_item` to recall what they already "
        "filtered, and `get_lens` / `set_lens` to read or tune the personal "
        "perspective every analysis is judged against."
    ),
    # Mounted as a sub-app at /mcp by main.py, so serve at this app's root.
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
)


def _split_analysis(analysis: dict) -> tuple[str, dict]:
    """Split analyzer output into (verdict, rest) — same shape the REST API uses."""
    analysis = dict(analysis or {})
    verdict = analysis.pop("verdict", "skim")
    return verdict, analysis


def _actions_for_user(analysis: dict, *, user_id: int, result, source_url: str) -> list[dict]:
    # Reuse the REST layer's action builder (profile + library-history aware)
    # so MCP and web hand out identical briefs. Imported lazily to keep this
    # module import-light for tests.
    from bot.api import _actions_for

    return _actions_for(
        analysis,
        user_id=user_id,
        source_text=result.summary,
        source_title=result.title,
        source_url=source_url,
    )


@mcp.tool()
async def analyze(url: str = "", text: str = "", note: str = "") -> dict:
    """Analyse a URL (article, YouTube, PDF, tweet…) or pasted text through the
    user's lens. Returns a verdict, why it matters, and 0–5 concrete next-step
    suggestions, and saves the result to the user's filter.fyi library.

    Provide exactly one of `url` or `text`. `note` is an optional remark from
    the user stored alongside the item. Long sources can take up to a minute.
    """
    user_id = _require_user_id()
    url = (url or "").strip()
    text = (text or "").strip()
    if bool(url) == bool(text):
        return {"error": "bad-request", "message": "Provide exactly one of `url` or `text`."}

    try:
        if url:
            result = await analyze_url(
                url,
                ctx=UsageContext(user_id=user_id, source_type="mcp"),
                save_for_user_id=user_id,
                user_note=note,
            )
        else:
            result = await analyze_text(
                text,
                ctx=UsageContext(user_id=user_id, source_type="mcp"),
                save_for_user_id=user_id,
                user_note=note,
            )
    except PipelineError as e:
        return {"error": e.code, "message": str(e) or "The analysis failed."}

    verdict, analysis = _split_analysis(result.analysis)
    return {
        "item_id": result.saved_id,
        "url": url or None,
        "title": result.title or url,
        "source_type": result.source_type,
        "verdict": verdict,
        "analysis": analysis,
        "actions": _actions_for_user(analysis, user_id=user_id, result=result, source_url=url),
    }


@mcp.tool()
async def search_library(query: str = "", limit: int = 20) -> list[dict]:
    """Search the user's filter.fyi library (everything they've analysed).
    Empty query returns the most recent items. Returns lean rows — use
    `get_library_item` for the full analysis of one item.
    """
    user_id = _require_user_id()
    limit = max(1, min(int(limit), 100))
    rows = await asyncio.to_thread(
        lambda: search_items(query, user_id) if query.strip() else get_all_items(user_id)
    )
    out = []
    for r in rows[:limit]:
        try:
            analysis = json.loads(r["analysis"] or "{}")
        except (TypeError, ValueError):
            analysis = {}
        out.append(
            {
                "item_id": r["id"],
                "title": analysis.get("title") or r["source"],
                "verdict": analysis.get("verdict", ""),
                "source": r["source"],
                "source_type": r["source_type"],
                "created_at": r["created_at"],
            }
        )
    return out


@mcp.tool()
async def get_library_item(item_id: int) -> dict:
    """Fetch one library item in full: stored summary (`content`), the complete
    analysis (main idea, why it matters, suggestions), and the user's note."""
    user_id = _require_user_id()
    row = await asyncio.to_thread(get_item, item_id, user_id)
    if not row:
        return {"error": "not-found", "message": f"No library item {item_id} for this user."}
    try:
        analysis = json.loads(row["analysis"] or "{}")
    except (TypeError, ValueError):
        analysis = {}
    return {
        "item_id": row["id"],
        "source": row["source"],
        "source_type": row["source_type"],
        "created_at": row["created_at"],
        "user_note": row["user_note"],
        "analysis": analysis,
        "content": row["content"],
    }


@mcp.tool()
async def get_lens() -> dict:
    """Read the user's lens — the personal perspective (role, goals, stack,
    interests) every analysis is judged against."""
    user_id = _require_user_id()
    profile = await asyncio.to_thread(get_user_profile, user_id)
    return {"lens": profile or "", "set": bool((profile or "").strip())}


@mcp.tool()
async def set_lens(lens: str) -> dict:
    """Replace the user's lens. Keep it short and concrete (who they are, what
    they're building, what they care about) — it steers every future verdict
    and suggestion. Read it first with `get_lens`; don't drop details the user
    still wants."""
    user_id = _require_user_id()
    lens = (lens or "").strip()
    if len(lens) > 4000:
        return {"error": "too-long", "message": "Keep the lens under 4000 characters."}
    await asyncio.to_thread(set_user_field, user_id, profile=lens)
    return {"ok": True, "lens": lens}


class _BearerAuthMiddleware:
    """Pure-ASGI bearer auth for the mounted MCP app.

    Validates `Authorization: Bearer <users.api_token>` and seeds the
    per-request ContextVar. Rejections are plain JSON 401s — cheap, and no MCP
    session is ever created for an unauthenticated caller.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        scheme, _, token = (headers.get("authorization") or "").partition(" ")
        row = None
        if scheme.lower() == "bearer" and token:
            row = await asyncio.to_thread(get_user_by_token, token)
        if not row:
            body = json.dumps(
                {"error": "unauthorized", "message": "Send Authorization: Bearer <api token> (Telegram /token)."}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token_ctx = _current_user_id.set(row["id"])
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user_id.reset(token_ctx)


def build_mcp_asgi_app():
    """The auth-wrapped Streamable-HTTP ASGI app, ready to mount at /mcp.

    The caller must also run `mcp.session_manager.run()` for the app's
    lifetime (main.py does this inside the FastAPI lifespan).
    """
    return _BearerAuthMiddleware(mcp.streamable_http_app())
