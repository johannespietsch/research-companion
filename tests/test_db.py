"""DB-layer tests: schema migration, user/item helpers, link codes, merge."""
from __future__ import annotations

import os
import sqlite3

import pytest


def _seed_old_schema(db_path: str) -> None:
    """Write a `profiles` + `items(user_id TEXT)` database the way pre-rebuild prod looked."""
    c = sqlite3.connect(db_path)
    c.executescript("""
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'unknown',
            source TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            analysis TEXT NOT NULL DEFAULT '',
            user_note TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );
        CREATE TABLE profiles (
            user_id TEXT PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            email TEXT UNIQUE,
            api_token TEXT UNIQUE
        );
        INSERT INTO profiles VALUES ('98765', 'I am a developer.', NULL, NULL);
        INSERT INTO profiles VALUES ('11111', 'I trade crypto.', 'real@tg.com', 'token_real');
        INSERT INTO profiles VALUES ('web:ghost@example.com', 'unused', 'ghost@example.com', 'token_ghost');
        INSERT INTO items (user_id, source_type, content, analysis) VALUES ('98765', 'note', 'hello A', '{}');
        INSERT INTO items (user_id, source_type, content, analysis) VALUES ('11111', 'url', 'hello B', '{}');
        INSERT INTO items (user_id, source_type, content, analysis) VALUES ('web:ghost@example.com', 'note', 'orphan web', '{}');
        INSERT INTO items (user_id, source_type, content, analysis) VALUES ('', 'note', 'pre-multiuser', '{}');
    """)
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# Migration from the old `profiles` schema
# ---------------------------------------------------------------------------

class TestMigration:
    def test_fresh_install_creates_users_and_items(self, db):
        # The autouse fixture runs _init() against a fresh dir; we should already
        # have empty users + items + link_codes tables.
        conn = db._get_conn()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "items", "link_codes"}.issubset(names)
        assert "profiles" not in names

    def test_migration_moves_telegram_users_only(self, monkeypatch):
        # Seed old schema, then import bot.db which triggers the migration
        data_dir = os.environ["DATA_DIR"]
        _seed_old_schema(os.path.join(data_dir, "research.db"))

        import bot.db as db

        conn = db._get_conn()
        users = list(conn.execute("SELECT telegram_chat_id, email, api_token, profile FROM users ORDER BY telegram_chat_id"))
        assert len(users) == 2, "web:<email> row should be dropped"
        assert users[0]["telegram_chat_id"] == 11111
        assert users[0]["email"] == "real@tg.com"
        assert users[0]["api_token"] == "token_real"
        assert users[0]["profile"] == "I trade crypto."
        assert users[1]["telegram_chat_id"] == 98765
        assert users[1]["profile"] == "I am a developer."

    def test_migration_remaps_item_user_ids(self, monkeypatch):
        data_dir = os.environ["DATA_DIR"]
        _seed_old_schema(os.path.join(data_dir, "research.db"))

        import bot.db as db

        conn = db._get_conn()
        items = list(conn.execute("SELECT id, user_id, content FROM items ORDER BY id"))
        # The web:<email> item and the empty-user-id item are dropped (orphans).
        assert len(items) == 2
        assert items[0]["content"] == "hello A"
        assert items[1]["content"] == "hello B"
        # Items should reference real users.id INTEGERs, not the old text.
        user_ids = {u["id"] for u in conn.execute("SELECT id FROM users")}
        for item in items:
            assert item["user_id"] in user_ids

    def test_migration_preserves_item_ids(self, monkeypatch):
        """`/show <id>` keeps working post-migration."""
        data_dir = os.environ["DATA_DIR"]
        _seed_old_schema(os.path.join(data_dir, "research.db"))

        import bot.db as db

        conn = db._get_conn()
        # Old items 1 and 2 had user_ids '98765' and '11111' — they should still
        # be items 1 and 2 with their content intact.
        rows = list(conn.execute("SELECT id, content FROM items ORDER BY id"))
        assert rows[0]["id"] == 1
        assert rows[0]["content"] == "hello A"
        assert rows[1]["id"] == 2
        assert rows[1]["content"] == "hello B"

    def test_init_is_idempotent(self, db):
        """Running _init twice on the new schema should be a no-op."""
        db._init()
        db._init()
        conn = db._get_conn()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "items", "link_codes"}.issubset(names)


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

class TestUserHelpers:
    def test_get_or_create_user_by_telegram(self, db):
        uid = db.get_or_create_user_by_telegram(12345)
        again = db.get_or_create_user_by_telegram(12345)
        assert uid == again

    def test_upsert_user_by_email_is_case_insensitive(self, db):
        a = db.upsert_user_by_email("Alice@Example.com")
        b = db.upsert_user_by_email("alice@example.com")
        c = db.upsert_user_by_email("  alice@example.com  ")
        assert a == b == c

    def test_set_and_get_user_profile(self, db):
        uid = db.get_or_create_user_by_telegram(1)
        assert db.get_user_profile(uid) == ""
        db.set_user_profile(uid, "I build chess engines.")
        assert db.get_user_profile(uid) == "I build chess engines."


# ---------------------------------------------------------------------------
# Link codes
# ---------------------------------------------------------------------------

class TestLinkCodes:
    def test_create_returns_6_digit_code(self, db):
        uid = db.upsert_user_by_email("a@b.com")
        code = db.create_link_code(uid)
        assert len(code) == 6
        assert code.isdigit()

    def test_redeem_returns_user_id_then_consumes(self, db):
        uid = db.upsert_user_by_email("a@b.com")
        code = db.create_link_code(uid)
        assert db.redeem_link_code(code) == uid
        assert db.redeem_link_code(code) is None, "second redemption must fail"

    def test_redeem_returns_none_for_invalid_code(self, db):
        assert db.redeem_link_code("000000") is None

    def test_one_outstanding_code_per_user(self, db):
        uid = db.upsert_user_by_email("a@b.com")
        first = db.create_link_code(uid)
        second = db.create_link_code(uid)
        # The first code is replaced by the second
        assert db.redeem_link_code(first) is None
        assert db.redeem_link_code(second) == uid


# ---------------------------------------------------------------------------
# link_telegram_to_user merge logic
# ---------------------------------------------------------------------------

class TestLinkTelegramMerge:
    def test_moves_items_from_telegram_to_web_user(self, db):
        web_uid = db.upsert_user_by_email("alice@example.com")
        tg_uid = db.get_or_create_user_by_telegram(555111)
        db.save_item(tg_uid, "note", "", "tg note", '{"verdict":"watch"}')

        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)

        assert db.get_user(tg_uid) is None, "orphan tg row deleted"
        web_items = db.get_all_items(web_uid)
        assert len(web_items) == 1
        assert web_items[0]["content"] == "tg note"

    def test_fills_empty_web_profile_from_telegram(self, db):
        web_uid = db.upsert_user_by_email("alice@example.com")
        tg_uid = db.get_or_create_user_by_telegram(555111)
        db.set_user_profile(tg_uid, "I love haiku.")

        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)

        assert db.get_user(web_uid)["profile"] == "I love haiku."

    def test_keeps_web_profile_when_set(self, db):
        web_uid = db.upsert_user_by_email("alice@example.com")
        db.set_user_profile(web_uid, "Already set on web.")
        tg_uid = db.get_or_create_user_by_telegram(555111)
        db.set_user_profile(tg_uid, "Conflicting tg version.")

        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)

        assert db.get_user(web_uid)["profile"] == "Already set on web."

    def test_attaches_telegram_chat_id(self, db):
        web_uid = db.upsert_user_by_email("alice@example.com")
        # No prior tg row at all — just attach
        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)
        assert db.get_user(web_uid)["telegram_chat_id"] == 555111

    def test_idempotent_relink(self, db):
        web_uid = db.upsert_user_by_email("alice@example.com")
        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)
        # Second call is a no-op (would raise on conflict otherwise)
        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)
        assert db.get_user(web_uid)["telegram_chat_id"] == 555111

    def test_raises_when_already_linked_to_different_chat(self, db):
        web_uid = db.upsert_user_by_email("alice@example.com")
        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)
        with pytest.raises(ValueError, match="already linked"):
            db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=999999)

    def test_raises_when_web_user_not_found(self, db):
        with pytest.raises(ValueError, match="not found"):
            db.link_telegram_to_user(web_user_id=999, telegram_chat_id=555111)

    def test_no_unique_conflict_when_tg_has_api_token_and_web_does_not(self, db):
        """Regression: prior code UPDATED web.api_token = tg.api_token BEFORE
        DELETEing tg, briefly violating users.api_token UNIQUE."""
        web_uid = db.upsert_user_by_email("alice@example.com")
        tg_uid = db.get_or_create_user_by_telegram(555111)
        db.set_user_field(tg_uid, api_token="tok_xyz")

        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)

        web_after = db.get_user(web_uid)
        assert web_after["telegram_chat_id"] == 555111
        assert web_after["api_token"] == "tok_xyz", "tg-side api_token should have moved over"
        assert db.get_user(tg_uid) is None, "orphan tg row deleted"

    def test_no_conflict_when_both_have_api_tokens(self, db):
        """When both rows already have a token, web's wins — tg's is discarded
        with its row (no UNIQUE violation either)."""
        web_uid = db.upsert_user_by_email("alice@example.com")
        db.set_user_field(web_uid, api_token="web_tok")
        tg_uid = db.get_or_create_user_by_telegram(555111)
        db.set_user_field(tg_uid, api_token="tg_tok")

        db.link_telegram_to_user(web_user_id=web_uid, telegram_chat_id=555111)

        assert db.get_user(web_uid)["api_token"] == "web_tok"
        assert db.get_user(tg_uid) is None


# ---------------------------------------------------------------------------
# URL cache
# ---------------------------------------------------------------------------

class TestUrlCache:
    def test_set_then_get_roundtrips_payload(self, db):
        payload = {"text": "hello", "title": "Hi", "source_type": "article", "image_urls": []}
        db.set_cached_fetch("https://example.com/a", payload)
        assert db.get_cached_fetch("https://example.com/a") == payload

    def test_get_misses_for_unknown_url(self, db):
        assert db.get_cached_fetch("https://nope.example/") is None

    def test_set_upserts_same_url(self, db):
        db.set_cached_fetch(
            "https://example.com/a",
            {"text": "v1", "title": "Hi", "source_type": "article"},
        )
        db.set_cached_fetch(
            "https://example.com/a",
            {"text": "v2", "title": "Hi2", "source_type": "article"},
        )
        assert db.get_cached_fetch("https://example.com/a")["text"] == "v2"

    def test_get_respects_max_age(self, db):
        """A row older than `max_age_seconds` is treated as a miss."""
        import sqlite3
        from datetime import datetime, timedelta, timezone

        db.set_cached_fetch(
            "https://example.com/a",
            {"text": "stale", "title": "", "source_type": "article"},
        )

        # Force fetched_at backwards in time.
        old = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("UPDATE url_cache SET fetched_at = ?", (old,))
        conn.commit()
        conn.close()

        assert db.get_cached_fetch("https://example.com/a", max_age_seconds=300) is None
        # Same row is fresh under a longer window.
        assert (
            db.get_cached_fetch("https://example.com/a", max_age_seconds=3600)["text"]
            == "stale"
        )


class TestPruneMaintenance:
    """prune_maintenance() bounds disk growth without touching live rows."""

    def test_prunes_old_rows_only(self, db):
        from datetime import datetime, timezone

        OLD = "2000-01-01T00:00:00"   # well past every retention window
        NEW = "2999-01-01T00:00:00"   # far future — always kept

        with db._get_conn() as conn:
            conn.execute(
                "INSERT INTO error_log (ts, logger, level, message) VALUES (?, 'x', 'WARNING', 'old')",
                (OLD,),
            )
            conn.execute(
                "INSERT INTO error_log (ts, logger, level, message) VALUES (?, 'x', 'WARNING', 'new')",
                (NEW,),
            )
            conn.execute(
                "INSERT INTO url_cache (url, payload, fetched_at) VALUES ('https://old', '{}', ?)",
                (OLD,),
            )
            conn.execute(
                "INSERT INTO url_cache (url, payload, fetched_at) VALUES ('https://new', '{}', ?)",
                (NEW,),
            )
            uid = conn.execute("INSERT INTO users (email) VALUES ('a@b.com')").lastrowid
            conn.execute(
                "INSERT INTO link_codes (code, user_id, expires_at) VALUES ('OLD', ?, ?)",
                (uid, OLD),
            )
            conn.execute(
                "INSERT INTO link_codes (code, user_id, expires_at) VALUES ('NEW', ?, ?)",
                (uid, NEW),
            )
            # Old terminal job → pruned. Old pending job → kept (task may still
            # be running). New terminal job → kept (within retention window).
            conn.execute(
                "INSERT INTO jobs (id, status, updated_at) VALUES ('old-done', 'done', ?)",
                (OLD,),
            )
            conn.execute(
                "INSERT INTO jobs (id, status, updated_at) VALUES ('old-pending', 'pending', ?)",
                (OLD,),
            )
            conn.execute(
                "INSERT INTO jobs (id, status, updated_at) VALUES ('new-done', 'done', ?)",
                (NEW,),
            )

        # Old + new processed_updates rows — old should prune (past 24h).
        with db._get_conn() as conn:
            conn.execute("INSERT INTO processed_updates (update_id, ts) VALUES (1, ?)", (OLD,))
            conn.execute("INSERT INTO processed_updates (update_id, ts) VALUES (2, ?)", (NEW,))

        counts = db.prune_maintenance(now=datetime(2026, 5, 20, tzinfo=timezone.utc))
        assert counts == {
            "error_log": 1, "url_cache": 1, "link_codes": 1, "jobs": 1,
            "processed_updates": 1,
        }

        with db._get_conn() as conn:
            assert [r["message"] for r in conn.execute("SELECT message FROM error_log")] == ["new"]
            assert [r["url"] for r in conn.execute("SELECT url FROM url_cache")] == ["https://new"]
            assert [r["code"] for r in conn.execute("SELECT code FROM link_codes")] == ["NEW"]
            assert {r["id"] for r in conn.execute("SELECT id FROM jobs")} == {"old-pending", "new-done"}
            assert [r["update_id"] for r in conn.execute("SELECT update_id FROM processed_updates")] == [2]

    def test_idempotent_on_empty(self, db):
        from datetime import datetime, timezone

        counts = db.prune_maintenance(now=datetime(2026, 5, 20, tzinfo=timezone.utc))
        assert counts == {
            "error_log": 0, "url_cache": 0, "link_codes": 0, "jobs": 0,
            "processed_updates": 0,
        }


class TestClaimTelegramUpdate:
    """First arrival of an update_id claims it; subsequent calls return False
    so the caller knows it's a retry and skips processing."""

    def test_first_claim_returns_true(self, db):
        assert db.claim_telegram_update(12345) is True

    def test_second_claim_for_same_id_returns_false(self, db):
        assert db.claim_telegram_update(12345) is True
        assert db.claim_telegram_update(12345) is False
        assert db.claim_telegram_update(12345) is False  # still false

    def test_distinct_ids_each_claim(self, db):
        assert db.claim_telegram_update(1) is True
        assert db.claim_telegram_update(2) is True
        assert db.claim_telegram_update(3) is True
        # And they all stay claimed
        assert db.claim_telegram_update(1) is False
        assert db.claim_telegram_update(2) is False

    def test_row_persists_for_dedup_window(self, db):
        db.claim_telegram_update(999)
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT update_id, ts FROM processed_updates WHERE update_id = 999"
            ).fetchone()
        assert row["update_id"] == 999
        assert row["ts"]  # default-stamped
