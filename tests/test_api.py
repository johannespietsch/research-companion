"""HTTP-level tests for the Worker-facing endpoints (shared-secret gated)."""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Auth (shared secret)
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_header_returns_401(self, client):
        r = client.post("/api/users/upsert", json={"email": "x@y.com"})
        assert r.status_code == 401

    def test_wrong_secret_returns_401(self, client):
        r = client.post(
            "/api/users/upsert",
            json={"email": "x@y.com"},
            headers={"x-filter-fyi-secret": "WRONG"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/users/upsert
# ---------------------------------------------------------------------------

class TestUserUpsert:
    def test_creates_user_and_is_idempotent(self, client, auth_headers):
        r1 = client.post("/api/users/upsert", json={"email": "Alice@Example.com"}, headers=auth_headers)
        r2 = client.post("/api/users/upsert", json={"email": "alice@example.com"}, headers=auth_headers)
        assert r1.status_code == 200
        assert r1.json()["user_id"] == r2.json()["user_id"]

    def test_rejects_invalid_email(self, client, auth_headers):
        r = client.post("/api/users/upsert", json={"email": "not-an-email"}, headers=auth_headers)
        assert r.status_code == 400


class TestUserGet:
    def test_returns_user_identifiers_no_api_token_leak(self, client, auth_headers, db):
        uid = client.post(
            "/api/users/upsert", json={"email": "alice@example.com"}, headers=auth_headers
        ).json()["user_id"]
        db.set_user_field(uid, api_token="secret_token_xyz")

        r = client.get(f"/api/users/{uid}", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == uid
        assert body["email"] == "alice@example.com"
        assert body["telegram_chat_id"] is None
        assert body["has_api_token"] is True
        assert "api_token" not in body, "raw token must not leak"

    def test_telegram_chat_id_reflects_linked_state(self, client, auth_headers, db):
        uid = client.post(
            "/api/users/upsert", json={"email": "bob@example.com"}, headers=auth_headers
        ).json()["user_id"]
        # Not linked yet
        assert client.get(f"/api/users/{uid}", headers=auth_headers).json()["telegram_chat_id"] is None

        # Link
        db.link_telegram_to_user(web_user_id=uid, telegram_chat_id=42)
        assert client.get(f"/api/users/{uid}", headers=auth_headers).json()["telegram_chat_id"] == 42

    def test_404_for_unknown_user(self, client, auth_headers):
        r = client.get("/api/users/99999", headers=auth_headers)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/library/add
# ---------------------------------------------------------------------------

class TestLibraryAdd:
    def _make_user(self, client, auth_headers, email="alice@example.com"):
        return client.post("/api/users/upsert", json={"email": email}, headers=auth_headers).json()["user_id"]

    def test_analyses_url_and_persists_item(self, client, auth_headers):
        uid = self._make_user(client, auth_headers)
        r = client.post(
            "/api/library/add",
            json={"user_id": uid, "url": "https://example.com/rag", "user_note": "found it"},
            headers=auth_headers,
        )
        assert r.status_code == 201
        body = r.json()
        assert body["verdict"] == "watch"
        assert body["id"] is not None
        assert "main_idea" in body["analysis"]
        # Agent-handoff actions: one per suggestion, each with a paste-able brief.
        actions = body["actions"]
        assert [a["index"] for a in actions] == [0, 1]
        assert all(a["title"] and a["brief"] and a["brief_link"] for a in actions)

    def test_rejects_invalid_url(self, client, auth_headers):
        uid = self._make_user(client, auth_headers)
        r = client.post(
            "/api/library/add",
            json={"user_id": uid, "url": "not-a-url"},
            headers=auth_headers,
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/library + GET /api/library/{id}
# ---------------------------------------------------------------------------

class TestLibrary:
    def _user_with_one_item(self, client, auth_headers):
        uid = client.post("/api/users/upsert", json={"email": "a@b.com"}, headers=auth_headers).json()["user_id"]
        added = client.post(
            "/api/library/add",
            json={"user_id": uid, "url": "https://example.com/x"},
            headers=auth_headers,
        ).json()
        return uid, added["id"]

    def test_list_returns_lean_shape(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        r = client.get(f"/api/library?user_id={uid}", headers=auth_headers)
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        row = rows[0]
        assert set(row.keys()) == {"id", "source_type", "source", "verdict", "title", "created_at"}
        assert row["verdict"] == "watch"
        assert "RAG" in row["title"]

    def test_show_returns_full_item(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        r = client.get(f"/api/library/{item_id}?user_id={uid}", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert "content" in body and "analysis" in body
        assert body["user_id"] == uid

    def test_show_404_for_wrong_user(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        r = client.get(f"/api/library/{item_id}?user_id=99999", headers=auth_headers)
        assert r.status_code == 404

    def test_stores_summary_not_full_fetched_text(self, client, auth_headers):
        # Data minimisation: items.content holds the condensed summary, never a
        # full copy of the fetched source.
        uid, item_id = self._user_with_one_item(client, auth_headers)
        item = client.get(f"/api/library/{item_id}?user_id={uid}", headers=auth_headers).json()
        assert item["content"] == "Neutral summary of the content."
        assert "Sample article body" not in item["content"]

    def test_delete_removes_item(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        r = client.delete(f"/api/library/{item_id}?user_id={uid}", headers=auth_headers)
        assert r.status_code == 204
        # Gone afterwards.
        assert client.get(f"/api/library/{item_id}?user_id={uid}", headers=auth_headers).status_code == 404
        assert client.get(f"/api/library?user_id={uid}", headers=auth_headers).json() == []

    def test_delete_404_for_wrong_user_leaves_item(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        r = client.delete(f"/api/library/{item_id}?user_id=99999", headers=auth_headers)
        assert r.status_code == 404
        # The real owner's item is untouched.
        assert client.get(f"/api/library/{item_id}?user_id={uid}", headers=auth_headers).status_code == 200

    def test_delete_requires_secret(self, client):
        r = client.delete("/api/library/1?user_id=1")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/users/{id}/export + DELETE /api/users/{id}  (GDPR portability + erasure)
# ---------------------------------------------------------------------------

class TestAccount:
    def _user_with_one_item(self, client, auth_headers, email="a@b.com"):
        uid = client.post("/api/users/upsert", json={"email": email}, headers=auth_headers).json()["user_id"]
        client.post(
            "/api/library/add",
            json={"user_id": uid, "url": "https://example.com/x"},
            headers=auth_headers,
        )
        return uid

    def test_export_returns_user_and_items_without_token(self, client, auth_headers):
        uid = self._user_with_one_item(client, auth_headers)
        r = client.get(f"/api/users/{uid}/export", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["user"]["id"] == uid
        assert body["user"]["email"] == "a@b.com"
        assert "api_token" not in body["user"]  # never leak the token
        assert len(body["items"]) == 1
        assert "content" in body["items"][0]

    def test_export_404_for_unknown_user(self, client, auth_headers):
        r = client.get("/api/users/99999/export", headers=auth_headers)
        assert r.status_code == 404

    def test_export_requires_secret(self, client):
        assert client.get("/api/users/1/export").status_code == 401

    def test_delete_purges_user_and_items(self, client, auth_headers):
        uid = self._user_with_one_item(client, auth_headers)
        r = client.delete(f"/api/users/{uid}", headers=auth_headers)
        assert r.status_code == 204
        # User gone, library empty, export 404s.
        assert client.get(f"/api/users/{uid}", headers=auth_headers).status_code == 404
        assert client.get(f"/api/library?user_id={uid}", headers=auth_headers).json() == []
        assert client.get(f"/api/users/{uid}/export", headers=auth_headers).status_code == 404

    def test_delete_is_idempotent(self, client, auth_headers):
        # Deleting a never-existed user still reports the desired end state.
        assert client.delete("/api/users/99999", headers=auth_headers).status_code == 204

    def test_delete_requires_secret(self, client):
        assert client.delete("/api/users/1").status_code == 401

    def test_delete_only_touches_target_user(self, client, auth_headers):
        keep = self._user_with_one_item(client, auth_headers, email="keep@b.com")
        drop = self._user_with_one_item(client, auth_headers, email="drop@b.com")
        assert client.delete(f"/api/users/{drop}", headers=auth_headers).status_code == 204
        # The other user's data is untouched.
        assert client.get(f"/api/users/{keep}", headers=auth_headers).status_code == 200
        assert len(client.get(f"/api/library?user_id={keep}", headers=auth_headers).json()) == 1


# ---------------------------------------------------------------------------
# PUT /api/users/{id}/profile  +  POST /api/feedback  (personalization plumbing)
# ---------------------------------------------------------------------------

class TestProfileWrite:
    def _make_user(self, client, auth_headers, email="p@b.com"):
        return client.post("/api/users/upsert", json={"email": email}, headers=auth_headers).json()["user_id"]

    def test_set_profile_roundtrips(self, client, auth_headers):
        uid = self._make_user(client, auth_headers)
        r = client.put(f"/api/users/{uid}/profile", json={"profile": "  I build chess engines.  "}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["profile"] == "I build chess engines."  # trimmed
        # Reflected in the user record.
        assert client.get(f"/api/users/{uid}", headers=auth_headers).json()["profile"] == "I build chess engines."

    def test_profile_is_capped(self, client, auth_headers):
        uid = self._make_user(client, auth_headers)
        from bot.api import PROFILE_MAX_CHARS
        r = client.put(f"/api/users/{uid}/profile", json={"profile": "x" * (PROFILE_MAX_CHARS + 500)}, headers=auth_headers)
        assert len(r.json()["profile"]) == PROFILE_MAX_CHARS

    def test_404_for_unknown_user(self, client, auth_headers):
        assert client.put("/api/users/99999/profile", json={"profile": "hi"}, headers=auth_headers).status_code == 404

    def test_requires_secret(self, client):
        assert client.put("/api/users/1/profile", json={"profile": "hi"}).status_code == 401


class TestFeedback:
    def _user_with_item(self, client, auth_headers, email="f@b.com"):
        uid = client.post("/api/users/upsert", json={"email": email}, headers=auth_headers).json()["user_id"]
        item_id = client.post(
            "/api/library/add",
            json={"user_id": uid, "url": "https://example.com/x"},
            headers=auth_headers,
        ).json()["id"]
        return uid, item_id

    def test_records_valid_signal(self, client, auth_headers):
        uid, item_id = self._user_with_item(client, auth_headers)
        r = client.post("/api/feedback", json={"user_id": uid, "item_id": item_id, "signal": "tried"}, headers=auth_headers)
        assert r.status_code == 201

    def test_rejects_unknown_signal(self, client, auth_headers):
        uid, item_id = self._user_with_item(client, auth_headers)
        r = client.post("/api/feedback", json={"user_id": uid, "item_id": item_id, "signal": "love-it"}, headers=auth_headers)
        assert r.status_code == 400

    def test_cannot_feedback_other_users_item(self, client, auth_headers):
        _, item_id = self._user_with_item(client, auth_headers, email="owner@b.com")
        other = client.post("/api/users/upsert", json={"email": "other@b.com"}, headers=auth_headers).json()["user_id"]
        r = client.post("/api/feedback", json={"user_id": other, "item_id": item_id, "signal": "tried"}, headers=auth_headers)
        assert r.status_code == 404

    def test_deleting_item_removes_its_feedback(self, client, auth_headers, db):
        uid, item_id = self._user_with_item(client, auth_headers)
        client.post("/api/feedback", json={"user_id": uid, "item_id": item_id, "signal": "tried"}, headers=auth_headers)
        client.delete(f"/api/library/{item_id}?user_id={uid}", headers=auth_headers)
        with db._get_conn() as conn:
            rows = conn.execute("SELECT 1 FROM feedback WHERE item_id = ?", (item_id,)).fetchall()
        assert rows == []

    def test_requires_secret(self, client):
        assert client.post("/api/feedback", json={"user_id": 1, "item_id": 1, "signal": "tried"}).status_code == 401


# ---------------------------------------------------------------------------
# POST /api/claim
# ---------------------------------------------------------------------------

class TestClaim:
    def test_inserts_batch_of_anon_rows(self, client, auth_headers):
        uid = client.post("/api/users/upsert", json={"email": "a@b.com"}, headers=auth_headers).json()["user_id"]
        r = client.post(
            "/api/claim",
            json={
                "user_id": uid,
                "rows": [
                    {"url": "https://a.com", "source_type": "article", "verdict": "skim",
                     "content_preview": "preview a", "analysis": {"main_idea": "A"}},
                    {"url": "https://b.com", "source_type": "youtube", "verdict": "skip",
                     "content_preview": "preview b", "analysis": {"main_idea": "B"}},
                ],
            },
            headers=auth_headers,
        )
        assert r.status_code == 201
        assert r.json()["count"] == 2

        listing = client.get(f"/api/library?user_id={uid}", headers=auth_headers).json()
        assert len(listing) == 2
        verdicts = {row["verdict"] for row in listing}
        assert verdicts == {"skim", "skip"}


# ---------------------------------------------------------------------------
# POST /api/link/start
# ---------------------------------------------------------------------------

class TestLinkStart:
    def test_returns_6_digit_code_with_expiry(self, client, auth_headers):
        uid = client.post("/api/users/upsert", json={"email": "a@b.com"}, headers=auth_headers).json()["user_id"]
        r = client.post("/api/link/start", json={"user_id": uid}, headers=auth_headers)
        assert r.status_code == 201
        body = r.json()
        assert len(body["code"]) == 6
        assert body["code"].isdigit()
        assert body["expires_in_seconds"] == 600


# ---------------------------------------------------------------------------
# POST /api/job + GET /api/job/{id}  (async analysis flow)
# ---------------------------------------------------------------------------

class TestJobFlow:
    def test_start_job_returns_job_id(self, client, auth_headers):
        r = client.post("/api/job", json={"url": "https://example.com/x"}, headers=auth_headers)
        assert r.status_code == 202
        assert isinstance(r.json().get("job_id"), str)

    def test_start_job_rejects_invalid_url(self, client, auth_headers):
        r = client.post("/api/job", json={"url": "not-a-url"}, headers=auth_headers)
        assert r.status_code == 400

    def test_start_job_requires_secret(self, client):
        r = client.post("/api/job", json={"url": "https://example.com/x"})
        assert r.status_code == 401

    def test_get_job_404_for_unknown(self, client, auth_headers):
        r = client.get("/api/job/unknown-id", headers=auth_headers)
        assert r.status_code == 404

    def test_run_job_signed_in_saves_item_and_completes(self, client, db, auth_headers):
        import asyncio
        import json
        import bot.api

        uid = client.post(
            "/api/users/upsert", json={"email": "j@b.com"}, headers=auth_headers
        ).json()["user_id"]
        db.create_job("job-1")
        asyncio.run(bot.api._run_job("job-1", "https://example.com/x", uid, "my note"))

        rec = db.get_job_record("job-1")
        assert rec["status"] == "done"
        result = json.loads(rec["result"])
        assert result["verdict"] == "watch"
        assert result["id"] is not None
        # Verdict is hoisted to the top level, not left inside analysis.
        assert "verdict" not in result["analysis"]
        # Item persisted for the signed-in user, storing the summary not raw text.
        items = db.get_all_items(uid)
        assert len(items) == 1
        assert items[0]["content"] == "Neutral summary of the content."

    def test_run_job_anonymous_does_not_save(self, client, db):
        import asyncio
        import json
        import bot.api

        db.create_job("job-2")
        asyncio.run(bot.api._run_job("job-2", "https://example.com/x", None, ""))

        rec = db.get_job_record("job-2")
        assert rec["status"] == "done"
        result = json.loads(rec["result"])
        assert "id" not in result
        assert db.get_all_items() == []

    # --- pasted-text path (the "paste it in" fallback) ---

    def test_start_job_accepts_text_without_url(self, client, auth_headers):
        r = client.post("/api/job",
                        json={"text": "A pasted reddit post about benchmarks."},
                        headers=auth_headers)
        assert r.status_code == 202
        assert isinstance(r.json().get("job_id"), str)

    def test_start_job_rejects_empty_text_and_url(self, client, auth_headers):
        r = client.post("/api/job", json={"text": "   "}, headers=auth_headers)
        assert r.status_code == 400

    def test_run_job_text_completes_without_fetching(self, client, db, monkeypatch):
        import asyncio
        import json
        import bot.pipeline

        async def boom(*a, **k):
            raise AssertionError("text job must not fetch")
        monkeypatch.setattr(bot.pipeline, "fetch_url", boom)

        import bot.api
        db.create_job("job-text")
        asyncio.run(bot.api._run_job("job-text", "", None, "", text="Gemini-SQL2 post\n\nbody"))

        rec = db.get_job_record("job-text")
        assert rec["status"] == "done"
        result = json.loads(rec["result"])
        assert result["verdict"] == "watch"
        assert result["url"] == ""              # no source URL for pasted text
        assert result["title"] == "Gemini-SQL2 post"
        assert result["source_type"] == "text"

    def test_run_job_text_signed_in_saves_item(self, client, db, auth_headers):
        import asyncio
        import bot.api

        uid = client.post("/api/users/upsert", json={"email": "t@b.com"},
                          headers=auth_headers).json()["user_id"]
        db.create_job("job-text2")
        asyncio.run(bot.api._run_job("job-text2", "", uid, "", text="pasted body"))

        items = db.get_all_items(uid)
        assert len(items) == 1
        assert items[0]["source"] == "" and items[0]["source_type"] == "text"

    def test_run_job_done_payload_exposes_full_summary(self, client, db):
        # The result page renders the full stored brief in a "what we read"
        # disclosure, so the done payload must carry `content` alongside the
        # short `content_preview` used by the Worker's D1 claim path.
        import asyncio
        import json
        import bot.api

        db.create_job("job-content")
        asyncio.run(bot.api._run_job("job-content", "https://example.com/x", None, ""))

        result = json.loads(db.get_job_record("job-content")["result"])
        assert result["content"] == "Neutral summary of the content."
        assert result["content_preview"] == "Neutral summary of the content."

    def test_run_job_analyzes_the_summary_not_raw_text(self, client, db, monkeypatch):
        # The verdict must be derived from the stored summary, not the raw
        # fetched text (the canonical-representation contract).
        import asyncio
        import bot.api
        import bot.pipeline

        captured = {}

        def capture_analyze(text, user_id=None, **_kwargs):
            captured["text"] = text
            return {
                "main_idea": "x", "why_it_matters": "y", "category": "c",
                "quick_win": "q", "bigger_play": "b", "time_required": "t",
                "verdict": "skim",
            }

        # Post-pipeline refactor: analyze + summarize_content live inside
        # bot.pipeline, so that's the module to patch for run-time overrides.
        monkeypatch.setattr(bot.pipeline, "analyze", capture_analyze)
        db.create_job("job-3")
        asyncio.run(bot.api._run_job("job-3", "https://example.com/x", None, ""))
        assert captured["text"] == "Neutral summary of the content."

    def test_run_job_no_text_sets_extraction_error(self, client, db, monkeypatch):
        import asyncio
        import bot.api
        import bot.pipeline

        async def empty_fetch(url, **kwargs):
            return {"text": "", "title": url, "source_type": "article", "reason": "no_text"}

        monkeypatch.setattr(bot.pipeline, "fetch_url", empty_fetch)
        db.create_job("job-4")
        asyncio.run(bot.api._run_job("job-4", "https://example.com/x", None, ""))
        rec = db.get_job_record("job-4")
        assert rec["status"] == "error"
        assert rec["error"] == "extraction-failed"
        # A user-facing explanation is persisted alongside the code so the
        # Worker can show *why* it failed, not just a generic line.
        assert rec["message"]

    def test_run_job_persists_specific_reason_message(self, client, db, monkeypatch):
        """A known fetch reason (e.g. paywall) surfaces its friendly message."""
        import asyncio
        import bot.api
        import bot.pipeline
        from bot import fetch_errors

        async def paywalled_fetch(url, **kwargs):
            return {"text": "", "title": url, "source_type": "article", "reason": fetch_errors.PAYWALLED}

        monkeypatch.setattr(bot.pipeline, "fetch_url", paywalled_fetch)
        db.create_job("job-pw")
        asyncio.run(bot.api._run_job("job-pw", "https://example.com/x", None, ""))
        rec = db.get_job_record("job-pw")
        assert rec["status"] == "error"
        assert rec["message"] == fetch_errors.user_message(fetch_errors.PAYWALLED, "https://example.com/x")
        assert "paywall" in rec["message"].lower()

    def test_get_job_status_returns_error_message(self, client, db):
        """The poll endpoint exposes the persisted message to the Worker."""
        db.create_job("job-st")
        db.set_job_error("job-st", "extraction-failed", "This looks paywalled.")
        resp = client.get("/api/job/job-st", headers={"x-filter-fyi-secret": "test-secret"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"] == "extraction-failed"
        assert body["message"] == "This looks paywalled."

    def test_run_job_video_no_text_sets_no_transcript(self, client, db, monkeypatch):
        import asyncio
        import bot.api
        import bot.pipeline

        async def empty_video_fetch(url, **kwargs):
            return {"text": "", "title": url, "source_type": "youtube", "reason": "no_transcript"}

        monkeypatch.setattr(bot.pipeline, "fetch_url", empty_video_fetch)
        db.create_job("job-5")
        asyncio.run(bot.api._run_job("job-5", "https://example.com/x", None, ""))
        rec = db.get_job_record("job-5")
        assert rec["status"] == "error"
        assert rec["error"] == "no-transcript"


# ---------------------------------------------------------------------------
# Shortlist (saved suggestions) — /api/saved-suggestions
# ---------------------------------------------------------------------------

class TestShortlist:
    def _user_with_one_item(self, client, auth_headers, email="s@b.com"):
        uid = client.post(
            "/api/users/upsert", json={"email": email}, headers=auth_headers
        ).json()["user_id"]
        item_id = client.post(
            "/api/library/add",
            json={"user_id": uid, "url": "https://example.com/x"},
            headers=auth_headers,
        ).json()["id"]
        return uid, item_id

    def _save(self, client, auth_headers, uid, item_id, index=0, **extra):
        body = {"user_id": uid, "item_id": item_id, "suggestion_index": index,
                "title": "Wire up a RAG demo", "detail": "Stand up a demo."}
        body.update(extra)
        return client.post("/api/saved-suggestions", json=body, headers=auth_headers)

    def test_save_creates_and_lists(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        r = self._save(client, auth_headers, uid, item_id)
        assert r.status_code == 201
        assert r.json()["status"] == "saved"

        rows = client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()
        assert len(rows) == 1
        row = rows[0]
        assert row["item_id"] == item_id
        assert row["suggestion_index"] == 0
        assert row["status"] == "saved"
        assert row["title"] == "Wire up a RAG demo"
        # Back-link fields from the joined item.
        assert row["source"] == "https://example.com/x"
        assert "RAG" in row["item_title"]

    def test_save_is_idempotent(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        first = self._save(client, auth_headers, uid, item_id).json()["id"]
        # Saving the same (item, index) again refreshes rather than duplicates.
        second = self._save(client, auth_headers, uid, item_id, detail="Refreshed.").json()["id"]
        assert first == second
        rows = client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()
        assert len(rows) == 1
        assert rows[0]["detail"] == "Refreshed."

    def test_distinct_indexes_are_separate_rows(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        self._save(client, auth_headers, uid, item_id, index=0)
        self._save(client, auth_headers, uid, item_id, index=1)
        rows = client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()
        assert {r["suggestion_index"] for r in rows} == {0, 1}

    def test_save_against_other_users_item_404(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        r = client.post(
            "/api/saved-suggestions",
            json={"user_id": 99999, "item_id": item_id, "suggestion_index": 0},
            headers=auth_headers,
        )
        assert r.status_code == 404

    def test_status_advances(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        saved_id = self._save(client, auth_headers, uid, item_id).json()["id"]
        r = client.patch(
            f"/api/saved-suggestions/{saved_id}",
            json={"user_id": uid, "status": "tried"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        rows = client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()
        assert rows[0]["status"] == "tried"

    def test_invalid_status_rejected(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        saved_id = self._save(client, auth_headers, uid, item_id).json()["id"]
        r = client.patch(
            f"/api/saved-suggestions/{saved_id}",
            json={"user_id": uid, "status": "bogus"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_status_update_wrong_user_404(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        saved_id = self._save(client, auth_headers, uid, item_id).json()["id"]
        r = client.patch(
            f"/api/saved-suggestions/{saved_id}",
            json={"user_id": 99999, "status": "done"},
            headers=auth_headers,
        )
        assert r.status_code == 404

    def test_delete_removes(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        saved_id = self._save(client, auth_headers, uid, item_id).json()["id"]
        r = client.delete(f"/api/saved-suggestions/{saved_id}?user_id={uid}", headers=auth_headers)
        assert r.status_code == 204
        assert client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json() == []

    def test_delete_wrong_user_404(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        saved_id = self._save(client, auth_headers, uid, item_id).json()["id"]
        r = client.delete(f"/api/saved-suggestions/{saved_id}?user_id=99999", headers=auth_headers)
        assert r.status_code == 404
        assert len(client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json()) == 1

    def test_deleting_item_cascades_shortlist(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        self._save(client, auth_headers, uid, item_id)
        client.delete(f"/api/library/{item_id}?user_id={uid}", headers=auth_headers)
        assert client.get(f"/api/saved-suggestions?user_id={uid}", headers=auth_headers).json() == []

    def test_export_includes_shortlist(self, client, auth_headers):
        uid, item_id = self._user_with_one_item(client, auth_headers)
        self._save(client, auth_headers, uid, item_id)
        export = client.get(f"/api/users/{uid}/export", headers=auth_headers).json()
        assert len(export["saved_suggestions"]) == 1
        assert export["saved_suggestions"][0]["item_id"] == item_id

    def test_requires_secret(self, client):
        r = client.get("/api/saved-suggestions?user_id=1")
        assert r.status_code == 401
