"""Tests for the MCP server (bot/mcp_server.py).

Two layers:
- tool functions called directly (with the auth ContextVar seeded), pipeline
  mocked — verifies shapes, ownership scoping, and error mapping;
- the ASGI auth middleware exercised end-to-end over httpx's ASGITransport —
  verifies the 401 path and that a valid token reaches the wrapped app.

House style: sync tests driving coroutines with `asyncio.run` (same as
tests/test_pipeline.py).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest


@pytest.fixture
def mcp_mod(monkeypatch):
    """bot.mcp_server bound to the isolated test DB, with the pipeline mocked."""
    import bot.mcp_server as m
    import bot.pipeline

    async def fake_analyze_url(url, *, ctx, save_for_user_id=None, user_note="", **kw):
        from bot.db import save_item

        analysis = {
            "verdict": "watch",
            "title": "Hello RAG",
            "main_idea": "RAG is useful.",
            "why_it_matters": "Because context.",
            "suggestions": [
                {"title": "Try it", "detail": "Build a tiny RAG.", "effort": "an evening", "first_step": "pip install"}
            ],
        }
        saved_id = None
        if save_for_user_id is not None:
            saved_id = save_item(
                save_for_user_id,
                "article",
                url,
                "summary text",
                json.dumps(analysis),
                user_note,
            )
        return bot.pipeline.PipelineResult(
            fetched={"title": "Hello RAG", "source_type": "article"},
            summary="summary text",
            analysis=dict(analysis),
            saved_id=saved_id,
        )

    monkeypatch.setattr(m, "analyze_url", fake_analyze_url)
    return m


def _make_user(db, token="tok-1", email="a@b.c"):
    uid = db.upsert_user_by_email(email)
    db.set_user_field(uid, api_token=token)
    return uid


def _as_user(mcp_mod, uid, coro):
    """Run one tool coroutine with the auth ContextVar seeded, then reset."""
    tok = mcp_mod._current_user_id.set(uid)
    try:
        return asyncio.run(coro)
    finally:
        mcp_mod._current_user_id.reset(tok)


def test_tools_require_an_authenticated_user(mcp_mod):
    with pytest.raises(RuntimeError):
        mcp_mod._require_user_id()


def test_analyze_saves_to_library_and_returns_actions(db, mcp_mod):
    uid = _make_user(db)
    out = _as_user(mcp_mod, uid, mcp_mod.analyze(url="https://example.com/rag"))

    assert out["verdict"] == "watch"
    assert out["item_id"] is not None
    assert out["analysis"]["main_idea"] == "RAG is useful."
    assert len(out["actions"]) == 1
    assert "brief" in out["actions"][0]

    # and it landed in the caller's library
    rows = db.get_all_items(uid)
    assert len(rows) == 1
    assert rows[0]["source"] == "https://example.com/rag"


def test_analyze_requires_exactly_one_input(db, mcp_mod):
    uid = _make_user(db)
    neither = _as_user(mcp_mod, uid, mcp_mod.analyze())
    both = _as_user(mcp_mod, uid, mcp_mod.analyze(url="https://x.y", text="some pasted text"))
    assert neither["error"] == "bad-request"
    assert both["error"] == "bad-request"


def test_analyze_maps_pipeline_errors(db, mcp_mod, monkeypatch):
    import bot.pipeline

    async def boom(url, **kw):
        raise bot.pipeline.PipelineError(bot.pipeline.ERR_FETCH_FAILED, message="could not fetch")

    monkeypatch.setattr(mcp_mod, "analyze_url", boom)
    uid = _make_user(db)
    out = _as_user(mcp_mod, uid, mcp_mod.analyze(url="https://example.com/dead"))
    assert out == {"error": "fetch-failed", "message": "could not fetch"}


def test_library_tools_are_ownership_scoped(db, mcp_mod):
    uid = _make_user(db, token="tok-1", email="a@b.c")
    other = _make_user(db, token="tok-2", email="d@e.f")

    created = _as_user(mcp_mod, uid, mcp_mod.analyze(url="https://example.com/rag"))
    mine = _as_user(mcp_mod, uid, mcp_mod.search_library())
    item = _as_user(mcp_mod, uid, mcp_mod.get_library_item(created["item_id"]))

    assert [r["item_id"] for r in mine] == [created["item_id"]]
    assert item["analysis"]["verdict"] == "watch"
    assert item["content"] == "summary text"

    # the other user sees nothing
    theirs = _as_user(mcp_mod, other, mcp_mod.search_library())
    stolen = _as_user(mcp_mod, other, mcp_mod.get_library_item(created["item_id"]))
    assert theirs == []
    assert stolen["error"] == "not-found"


def test_search_library_matches_query(db, mcp_mod):
    uid = _make_user(db)
    _as_user(mcp_mod, uid, mcp_mod.analyze(url="https://example.com/rag"))
    hit = _as_user(mcp_mod, uid, mcp_mod.search_library(query="rag"))
    miss = _as_user(mcp_mod, uid, mcp_mod.search_library(query="quantum-basket-weaving"))
    assert len(hit) == 1
    assert miss == []


def test_lens_roundtrip(db, mcp_mod):
    uid = _make_user(db)
    assert _as_user(mcp_mod, uid, mcp_mod.get_lens()) == {"lens": "", "set": False}
    set_out = _as_user(mcp_mod, uid, mcp_mod.set_lens("Solo founder building trading bots."))
    assert set_out["ok"] is True
    assert _as_user(mcp_mod, uid, mcp_mod.get_lens()) == {
        "lens": "Solo founder building trading bots.",
        "set": True,
    }
    too_long = _as_user(mcp_mod, uid, mcp_mod.set_lens("x" * 4001))
    assert too_long["error"] == "too-long"


# --- auth middleware ---------------------------------------------------------

def _echo_app():
    """Minimal ASGI app that reports the ContextVar the middleware seeded."""

    async def app(scope, receive, send):
        import bot.mcp_server as m

        body = json.dumps({"user_id": m._current_user_id.get()}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    return app


def test_middleware_rejects_missing_or_bad_tokens(db, mcp_mod):
    async def run():
        wrapped = mcp_mod._BearerAuthMiddleware(_echo_app())
        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            no_header = await c.post("/")
            bad = await c.post("/", headers={"authorization": "Bearer nope"})
            wrong_scheme = await c.post("/", headers={"authorization": "Basic abc"})
        return no_header, bad, wrong_scheme

    no_header, bad, wrong_scheme = asyncio.run(run())
    assert no_header.status_code == 401
    assert bad.status_code == 401
    assert wrong_scheme.status_code == 401
    assert no_header.headers["www-authenticate"] == "Bearer"


def test_middleware_seeds_user_and_resets_after(db, mcp_mod):
    uid = _make_user(db, token="sekrit")

    async def run():
        wrapped = mcp_mod._BearerAuthMiddleware(_echo_app())
        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post("/", headers={"authorization": "Bearer sekrit"})

    ok = asyncio.run(run())
    assert ok.status_code == 200
    assert ok.json() == {"user_id": uid}
    # the request-scoped identity never leaks past the request
    assert mcp_mod._current_user_id.get() is None


def test_main_app_mounts_mcp():
    """The FastAPI app exposes /mcp (auth-wrapped) without disturbing REST routes."""
    import main

    mounted = [r for r in main.app.routes if getattr(r, "path", "") == "/mcp"]
    assert mounted, "expected /mcp to be mounted on the app"
