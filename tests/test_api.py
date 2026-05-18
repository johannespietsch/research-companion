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
