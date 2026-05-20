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
