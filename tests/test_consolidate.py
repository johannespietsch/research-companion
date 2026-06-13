"""Cross-source consolidation (#70): similarity, merge-on-save, multi-source
briefs, and digest clustering.

The product contract: when several sources converge on the same next step,
the user sees ONE action backed by all of them — on the Shortlist (merge at
save time) and in the weekly digest (cluster at assembly time). Similarity is
deliberately conservative: distinct-but-related suggestions must NOT merge.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 6, 12, 6, 0, tzinfo=timezone.utc)


class TestSimilarity:
    def test_rewordings_of_the_same_step_match(self):
        from bot.consolidate import SIMILARITY_THRESHOLD, similarity
        a = "Set up an eval harness for your RAG pipeline"
        b = "Build an eval harness covering the RAG pipeline"
        assert similarity(a, b) >= SIMILARITY_THRESHOLD

    def test_distinct_steps_do_not_match(self):
        from bot.consolidate import SIMILARITY_THRESHOLD, similarity
        a = "Set up an eval harness for your RAG pipeline"
        b = "Migrate the trading bot to async websockets"
        assert similarity(a, b) < SIMILARITY_THRESHOLD

    def test_empty_text_never_matches(self):
        from bot.consolidate import similarity
        assert similarity("", "anything at all") == 0.0

    def test_find_similar_returns_best_match_or_none(self):
        from bot.consolidate import find_similar
        candidates = [
            {"title": "Migrate to websockets", "detail": "async trading bot rewrite"},
            {"title": "Eval harness", "detail": "set up an eval harness for the RAG pipeline"},
        ]
        hit = find_similar("Build eval harness", "an eval harness for your RAG pipeline", candidates)
        assert hit is candidates[1]
        assert find_similar("Write a novel", "fiction in your spare time", candidates) is None

    def test_cluster_groups_convergent_and_preserves_order(self):
        from bot.consolidate import cluster
        items = [
            {"title": "Eval harness", "detail": "set up an eval harness for the RAG pipeline"},
            {"title": "Websockets", "detail": "migrate the trading bot to async websockets"},
            {"title": "Build eval harness", "detail": "an eval harness for your RAG pipeline"},
        ]
        groups = cluster(items)
        assert [len(g) for g in groups] == [2, 1]
        assert groups[0][0]["title"] == "Eval harness"  # first occurrence leads


def _save(client, auth_headers, uid, item_id, *, index=0, title, detail):
    return client.post("/api/saved-suggestions", headers=auth_headers, json={
        "user_id": uid, "item_id": item_id, "suggestion_index": index,
        "title": title, "detail": detail, "effort": "", "first_step": "", "grounded_in": "",
    })


class TestMergeOnSave:
    @pytest.fixture
    def user_with_items(self, db):
        uid = db.upsert_user_by_email("u@example.com")
        a = db.save_item(uid, "article", "https://ex.com/a", "c", json.dumps({"main_idea": "Read A", "verdict": "watch"}))
        b = db.save_item(uid, "article", "https://ex.com/b", "c", json.dumps({"main_idea": "Read B", "verdict": "watch"}))
        return uid, a, b

    def test_similar_save_from_another_item_merges(self, client, auth_headers, db, user_with_items):
        uid, a, b = user_with_items
        r1 = _save(client, auth_headers, uid, a,
                   title="Eval harness", detail="set up an eval harness for the RAG pipeline")
        assert r1.status_code == 201 and "merged" not in r1.json()
        r2 = _save(client, auth_headers, uid, b,
                   title="Build eval harness", detail="an eval harness for your RAG pipeline")
        assert r2.json() == {"id": r1.json()["id"], "status": "saved", "merged": True}

        rows = client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()
        assert len(rows) == 1
        assert [s["source"] for s in rows[0]["sources"]] == ["https://ex.com/b"]
        assert rows[0]["sources"][0]["item_title"] == "Read B"

    def test_dissimilar_save_appends_normally(self, client, auth_headers, db, user_with_items):
        uid, a, b = user_with_items
        _save(client, auth_headers, uid, a,
              title="Eval harness", detail="set up an eval harness for the RAG pipeline")
        r = _save(client, auth_headers, uid, b,
                  title="Websockets", detail="migrate the trading bot to async websockets")
        assert "merged" not in r.json()
        rows = client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()
        assert len(rows) == 2

    def test_resave_of_same_suggestion_stays_idempotent_not_merged(self, client, auth_headers, db, user_with_items):
        uid, a, _ = user_with_items
        r1 = _save(client, auth_headers, uid, a, title="Eval harness",
                   detail="set up an eval harness for the RAG pipeline")
        r2 = _save(client, auth_headers, uid, a, title="Eval harness",
                   detail="set up an eval harness for the RAG pipeline")
        assert r2.json()["id"] == r1.json()["id"]
        assert "merged" not in r2.json()

    def test_merge_is_idempotent_across_repeat_saves(self, client, auth_headers, db, user_with_items):
        uid, a, b = user_with_items
        _save(client, auth_headers, uid, a, title="Eval harness",
              detail="set up an eval harness for the RAG pipeline")
        for _ in range(2):
            _save(client, auth_headers, uid, b, title="Build eval harness",
                  detail="an eval harness for your RAG pipeline")
        rows = client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()
        assert len(rows) == 1 and len(rows[0]["sources"]) == 1

    def test_deleting_the_entry_drops_its_sources(self, client, auth_headers, db, user_with_items):
        uid, a, b = user_with_items
        r = _save(client, auth_headers, uid, a, title="Eval harness",
                  detail="set up an eval harness for the RAG pipeline")
        _save(client, auth_headers, uid, b, title="Build eval harness",
              detail="an eval harness for your RAG pipeline")
        client.delete(f"/api/saved-suggestions/{r.json()['id']}?user_id={uid}", headers=auth_headers)
        with db._get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM saved_suggestion_sources").fetchone()[0] == 0

    def test_erasure_covers_sources(self, client, auth_headers, db, user_with_items):
        uid, a, b = user_with_items
        _save(client, auth_headers, uid, a, title="Eval harness",
              detail="set up an eval harness for the RAG pipeline")
        _save(client, auth_headers, uid, b, title="Build eval harness",
              detail="an eval harness for your RAG pipeline")
        db.delete_user(uid)
        with db._get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM saved_suggestion_sources").fetchone()[0] == 0


class TestMultiSourceBrief:
    def test_extra_sources_land_inside_the_fenced_block(self):
        from bot.agent_brief import build_agent_brief
        brief = build_agent_brief(
            action="Set up an eval harness",
            source_title="Read A", source_url="https://ex.com/a",
            extra_sources=[{"title": "Read B", "url": "https://ex.com/b"}],
        )
        body = brief.split("--- SOURCE")[1].split("--- END SOURCE")[0]
        assert "Read A — https://ex.com/a" in body
        assert "Also recommended by: Read B — https://ex.com/b" in body


class TestDigestClustering:
    @pytest.fixture
    def digest_env(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test")
        monkeypatch.setenv("DIGEST_FROM_EMAIL", "digest@filter.fyi")
        monkeypatch.setenv("DIGEST_UNSUBSCRIBE_SECRET", "s")
        monkeypatch.setenv("WEBHOOK_URL", "https://backend.test")

    def _item(self, db, uid, *, main_idea, source, title, detail):
        analysis = {"main_idea": main_idea, "why_it_matters": "", "grounded_in": "g",
                    "category": "ai", "time_required": "5 min", "verdict": "watch",
                    "suggestions": [{"title": title, "detail": detail, "effort": "", "first_step": ""}]}
        db.save_item(uid, "article", source, "c", json.dumps(analysis))

    def test_convergent_week_collapses_to_one_backed_action(self, db, digest_env):
        from bot.digest import build_digest, render_digest_text
        uid = db.upsert_user_by_email("u@example.com")
        self._item(db, uid, main_idea="Read A", source="https://ex.com/a",
                   title="Eval harness", detail="set up an eval harness for the RAG pipeline")
        self._item(db, uid, main_idea="Read B", source="https://ex.com/b",
                   title="Build eval harness", detail="an eval harness for your RAG pipeline")
        self._item(db, uid, main_idea="Read C", source="https://ex.com/c",
                   title="Websockets", detail="migrate the trading bot to async websockets")
        d = build_digest(uid, now=NOW)
        assert len(d["actions"]) == 2
        merged = next(a for a in d["actions"] if a["also_from"])
        assert {x["url"] for x in merged["also_from"]} == {"https://ex.com/a"} or \
               {x["url"] for x in merged["also_from"]} == {"https://ex.com/b"}
        assert "Also recommended by:" in merged["brief"]
        assert "Also recommended by:" in render_digest_text(d)
