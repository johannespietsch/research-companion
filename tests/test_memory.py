"""Memory-infused handoff briefs (#71): the per-suggestion library-history
block and its wiring into build_actions / the handoff brief.

Contracts:
  1. Anon users (user_id=None) get an empty block — briefs unchanged.
  2. History is scoped to *related* suggestions only (topic overlap), so
     unrelated library items never leak into a brief.
  3. The block sits in the instruction region, NOT inside the fenced SOURCE
     block (it's the user's own memory, not untrusted source material).
  4. A history-lookup failure degrades to no-history, never breaks the brief.
"""
from __future__ import annotations

import pytest


def _save(db, uid, item_id, *, index, title, detail, status="saved"):
    sid = db.save_suggestion(user_id=uid, item_id=item_id, suggestion_index=index,
                             title=title, detail=detail, effort="", first_step="",
                             grounded_in="")
    if status != "saved":
        db.update_saved_suggestion_status(sid, uid, status)
    return sid


class TestBuildHistoryBlock:
    def test_anon_gets_nothing(self, db):
        from bot.memory import build_history_block
        assert build_history_block(None, title="Add a reranker", detail="to the RAG pipeline") == ""

    def test_empty_library_gets_nothing(self, db):
        from bot.memory import build_history_block
        uid = db.upsert_user_by_email("u@example.com")
        assert build_history_block(uid, title="Add a reranker", detail="to the RAG pipeline") == ""

    def test_related_outcomes_and_dismissals_appear(self, db):
        from bot.memory import build_history_block
        uid = db.upsert_user_by_email("u@example.com")
        item = db.save_item(uid, "article", "https://ex.com/a", "c", "{}")
        _save(db, uid, item, index=0, title="Set up an eval harness",
              detail="eval harness for the RAG pipeline", status="done")
        _save(db, uid, item, index=1, title="Add a reranker",
              detail="reranker on the RAG pipeline retrieval", status="tried")
        db.record_suggestion_signal(uid, "dismiss",
                                    suggestion_text="Build a RAG pipeline demo app",
                                    reason="too generic")
        block = build_history_block(uid, title="Improve RAG pipeline",
                                    detail="make the RAG pipeline retrieval better")
        assert "Already done:" in block and "eval harness" in block.lower()
        assert "Tried:" in block and "reranker" in block.lower()
        assert "Decided against:" in block and "too generic" in block

    def test_unrelated_history_is_excluded(self, db):
        from bot.memory import build_history_block
        uid = db.upsert_user_by_email("u@example.com")
        item = db.save_item(uid, "article", "https://ex.com/a", "c", "{}")
        _save(db, uid, item, index=0, title="Migrate trading bot",
              detail="move the trading bot to async websockets", status="done")
        assert build_history_block(uid, title="Add a reranker",
                                   detail="reranker for the RAG pipeline") == ""

    def test_the_action_itself_is_not_echoed_as_history(self, db):
        from bot.memory import build_history_block
        uid = db.upsert_user_by_email("u@example.com")
        item = db.save_item(uid, "article", "https://ex.com/a", "c", "{}")
        # An identical prior save shouldn't be reported back as "already done".
        _save(db, uid, item, index=0, title="Add a reranker",
              detail="add a reranker to the RAG pipeline", status="done")
        block = build_history_block(uid, title="Add a reranker",
                                    detail="add a reranker to the RAG pipeline")
        assert block == ""

    def test_block_is_bounded(self, db):
        from bot.memory import HISTORY_CHARS, build_history_block
        uid = db.upsert_user_by_email("u@example.com")
        item = db.save_item(uid, "article", "https://ex.com/a", "c", "{}")
        for i in range(20):
            _save(db, uid, item, index=i,
                  title=f"RAG pipeline improvement {i} " + "x" * 80,
                  detail="work on the RAG pipeline retrieval " + "y" * 80, status="done")
        assert len(build_history_block(uid, title="RAG pipeline",
                                       detail="improve the RAG pipeline retrieval")) <= HISTORY_CHARS


class TestBriefIntegration:
    def test_history_is_outside_the_source_fence(self, db):
        from bot.agent_brief import build_agent_brief
        brief = build_agent_brief(
            action="Improve the RAG pipeline",
            source_title="A", source_url="https://ex.com/a",
            history='From my own library — relevant things I\'ve already acted on:\n- Already done: "Eval harness"',
        )
        before_fence = brief.split("--- SOURCE")[0]
        assert "Already done" in before_fence  # in the instruction region
        fenced = brief.split("--- SOURCE")[1].split("--- END SOURCE")[0]
        assert "Already done" not in fenced

    def test_build_actions_threads_history_per_suggestion(self, db):
        from bot.agent_brief import build_actions
        analysis = {"grounded_in": "g", "suggestions": [
            {"title": "Add a reranker", "detail": "to the pipeline", "effort": "", "first_step": ""},
            {"title": "Write a blog post", "detail": "about it", "effort": "", "first_step": ""},
        ]}
        seen = []

        def history_for(title, detail):
            seen.append(title)
            return "HIST::" + title if title == "Add a reranker" else ""

        actions = build_actions(analysis, history_for=history_for)
        assert seen == ["Add a reranker", "Write a blog post"]
        assert "HIST::Add a reranker" in actions[0]["brief"]
        assert "HIST::" not in actions[1]["brief"]

    def test_no_history_callable_leaves_brief_unchanged(self, db):
        from bot.agent_brief import build_actions
        analysis = {"grounded_in": "", "suggestions": [
            {"title": "Do X", "detail": "detail", "effort": "", "first_step": ""}]}
        actions = build_actions(analysis)
        assert "From my own library" not in actions[0]["brief"]


class TestActionsForWiring:
    def test_signed_in_brief_carries_history(self, db, monkeypatch):
        from bot import api
        uid = db.upsert_user_by_email("u@example.com")
        item = db.save_item(uid, "article", "https://ex.com/a", "c", "{}")
        db.save_suggestion(user_id=uid, item_id=item, suggestion_index=0,
                           title="Set up an eval harness",
                           detail="eval harness for the RAG pipeline",
                           effort="", first_step="", grounded_in="")
        db.update_saved_suggestion_status(
            db.get_saved_suggestions(uid)[0]["id"], uid, "done")
        analysis = {"grounded_in": "g", "suggestions": [
            {"title": "Improve RAG retrieval", "detail": "tune the RAG pipeline retrieval",
             "effort": "", "first_step": ""}]}
        actions = api._actions_for(analysis, user_id=uid, source_text="x",
                                   source_title="T", source_url="https://ex.com/b")
        assert "From my own library" in actions[0]["brief"]

    def test_anon_brief_has_no_history(self, db):
        from bot import api
        analysis = {"grounded_in": "g", "suggestions": [
            {"title": "Improve RAG retrieval", "detail": "tune retrieval",
             "effort": "", "first_step": ""}]}
        actions = api._actions_for(analysis, user_id=None, source_text="x")
        assert "From my own library" not in actions[0]["brief"]

    def test_history_failure_does_not_break_actions(self, db, monkeypatch):
        from bot import api
        import bot.memory
        uid = db.upsert_user_by_email("u@example.com")
        monkeypatch.setattr(bot.memory, "build_history_block",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        analysis = {"grounded_in": "g", "suggestions": [
            {"title": "Do X", "detail": "d", "effort": "", "first_step": ""}]}
        actions = api._actions_for(analysis, user_id=uid, source_text="x")
        assert actions and "From my own library" not in actions[0]["brief"]
