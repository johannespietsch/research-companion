"""Shared pytest fixtures.

`bot/db.py` reads `DATA_DIR` at import time and runs `_init()`, so we have to
set it (and the try-secret) **before** any `bot.*` import. The `autouse`
fixture below handles that for every test, with a fresh temp directory and
a cleanly re-imported `bot.db` module per test.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    """Point bot.db at a fresh DATA_DIR for every test and reload the module.

    Reloading is necessary because `bot.db._init()` runs at import time. Without
    a reload, the second test would reuse the connection bound to the first
    test's temp file.
    """
    tmp = tempfile.mkdtemp(prefix="filter-fyi-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("FILTER_FYI_TRY_SECRET", "test-secret")

    # Drop any cached bot.* modules so the next import sees the new DATA_DIR
    for mod in list(sys.modules):
        if mod == "bot" or mod.startswith("bot."):
            del sys.modules[mod]


@pytest.fixture
def db():
    """Fresh `bot.db` module bound to the isolated DATA_DIR."""
    import bot.db
    return bot.db


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient with the LLM + fetch path mocked.

    Post the bot.pipeline refactor, `fetch_url`, `analyze`, and
    `summarize_content` are no longer imported into bot.api — they live
    inside bot.pipeline. Tests that need to override one of these should
    patch `bot.pipeline.<name>` rather than `bot.api.<name>`.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import bot.api
    import bot.pipeline

    async def fake_fetch_url(url):
        return {
            "text": "Sample article body about retrieval-augmented generation.",
            "title": "Hello RAG",
            "source_type": "article",
            "image_urls": [],
        }

    def fake_analyze(text, user_id=None, **_kwargs):
        return {
            "main_idea": "RAG = retrieval-augmented generation.",
            "why_it_matters": "Practical AI pattern.",
            "grounded_in": "They show a 12-point eval lift from reranking retrieved chunks.",
            "category": "ai-engineering",
            "quick_win": "Wire up a 20-doc RAG demo this afternoon.",
            "first_step": "Create rag_demo.py and load 20 markdown files into a vector store.",
            "bigger_play": "Build an evaluated RAG pipeline over your own corpus.",
            "time_required": "10 min read",
            "verdict": "watch",
        }

    def fake_summarize_content(text, **_kwargs):
        return "Neutral summary of the content."

    # Pipeline's external dependencies — replace the names as the pipeline
    # module sees them.
    monkeypatch.setattr(bot.pipeline, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(bot.pipeline, "analyze", fake_analyze)
    monkeypatch.setattr(bot.pipeline, "summarize_content", fake_summarize_content)
    # /submit/text still calls analyze directly in bot.api — patch there too.
    monkeypatch.setattr(bot.api, "analyze", fake_analyze)

    app = FastAPI()
    app.include_router(bot.api.router)
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"x-filter-fyi-secret": "test-secret"}
