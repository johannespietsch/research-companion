import os
import sqlite3
from pathlib import Path

# DATA_DIR lets us point at a mounted volume in prod (Fly) while keeping the
# default of "project root" so local dev / tests keep working unchanged.
_DATA_DIR = Path(os.getenv("DATA_DIR") or (Path(__file__).parent.parent))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DATA_DIR / "research.db"


# Canonical identity. One row per person; both web (email) and Telegram
# (telegram_chat_id) faces hang off the same `id`. `profile` is the
# perspective text fed to the analyzer ("about this person"). When a user
# eventually has multiple profiles, this column moves to a separate
# `profiles` table (id, user_id, name, content) with one row per profile.
_CREATE_USERS_SQL = """\
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id INTEGER UNIQUE,
    email            TEXT UNIQUE,
    api_token        TEXT UNIQUE,
    profile          TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)"""

_CREATE_ITEMS_SQL = """\
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'unknown',
    source      TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    analysis    TEXT NOT NULL DEFAULT '',
    user_note   TEXT NOT NULL DEFAULT '',
    file_path   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
)"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id, created_at DESC)",
]


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _migrate_from_profiles(conn: sqlite3.Connection) -> None:
    """One-time migration: profiles + items(user_id TEXT) -> users + items(user_id INT).

    - Telegram users (numeric profiles.user_id) move to users.telegram_chat_id.
    - `web:<email>` rows are dropped — that codepath was never reached in prod.
    - Items whose user_id can't be mapped are dropped — they predate multi-user
      (NOT NULL DEFAULT '' rows) and have no owner in the new model.
    """
    conn.execute(_CREATE_USERS_SQL)

    id_map: dict[str, int] = {}
    old_profiles = conn.execute(
        "SELECT user_id, content, email, api_token FROM profiles"
    ).fetchall()
    for r in old_profiles:
        old_id = (r["user_id"] or "").strip()
        if not old_id.isdigit():
            continue  # skip web:<email> + any other non-Telegram rows
        cur = conn.execute(
            "INSERT INTO users (telegram_chat_id, email, api_token, profile) "
            "VALUES (?, ?, ?, ?)",
            (int(old_id), r["email"], r["api_token"], r["content"] or ""),
        )
        id_map[old_id] = cur.lastrowid

    conn.execute("ALTER TABLE items RENAME TO _items_old")
    conn.execute(_CREATE_ITEMS_SQL)

    old_items = conn.execute(
        "SELECT id, user_id, source_type, source, content, analysis, "
        "user_note, file_path, created_at FROM _items_old"
    ).fetchall()
    for r in old_items:
        new_uid = id_map.get((r["user_id"] or "").strip())
        if new_uid is None:
            continue
        conn.execute(
            "INSERT INTO items (id, user_id, source_type, source, content, "
            "analysis, user_note, file_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"], new_uid, r["source_type"], r["source"], r["content"],
                r["analysis"], r["user_note"], r["file_path"], r["created_at"],
            ),
        )

    conn.execute("DROP TABLE _items_old")
    conn.execute("DROP TABLE profiles")


def _init() -> None:
    with _get_conn() as conn:
        if _has_table(conn, "users"):
            # Already on the new schema; ensure tables/indexes exist (idempotent).
            conn.execute(_CREATE_USERS_SQL)
            conn.execute(_CREATE_ITEMS_SQL)
        elif _has_table(conn, "profiles"):
            _migrate_from_profiles(conn)
        else:
            conn.execute(_CREATE_USERS_SQL)
            conn.execute(_CREATE_ITEMS_SQL)
        for stmt in _CREATE_INDEXES_SQL:
            conn.execute(stmt)


_init()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_or_create_user_by_telegram(telegram_chat_id: int) -> int:
    """Return the canonical users.id for a Telegram chat. Creates a row if missing."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_chat_id = ?", (telegram_chat_id,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (telegram_chat_id) VALUES (?)", (telegram_chat_id,)
        )
        return cur.lastrowid


def upsert_user_by_email(email: str) -> int:
    """Get or create a user keyed by email (used by the magic-link flow)."""
    email = email.lower().strip()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute("INSERT INTO users (email) VALUES (?)", (email,))
        return cur.lastrowid


def get_user(user_id: int) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, telegram_chat_id, email, api_token, profile, created_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def get_user_by_token(token: str) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, telegram_chat_id, email, api_token, profile, created_at "
            "FROM users WHERE api_token = ?",
            (token,),
        ).fetchone()


def get_user_profile(user_id: int) -> str:
    """The user's profile text — perspective fed to the analyzer."""
    row = get_user(user_id)
    return row["profile"] if row else ""


def set_user_profile(user_id: int, profile: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET profile = ? WHERE id = ?", (profile, user_id)
        )


def set_user_field(user_id: int, **fields) -> None:
    """Update arbitrary user columns (email, api_token, profile)."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",
            list(fields.values()) + [user_id],
        )


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def save_item(
    user_id: int,
    source_type: str,
    source: str,
    content: str,
    analysis: str,
    user_note: str = "",
    *,
    file_path: str = "",
) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO items (user_id, source_type, source, content, "
            "analysis, user_note, file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, source_type, source, content, analysis, user_note, file_path),
        )


_ITEM_COLS = (
    "id, user_id, source_type, source, content, analysis, user_note, file_path, created_at"
)


def get_all_items(user_id: int | None = None) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        if user_id is not None:
            return conn.execute(
                f"SELECT {_ITEM_COLS} FROM items WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        return conn.execute(
            f"SELECT {_ITEM_COLS} FROM items ORDER BY id DESC"
        ).fetchall()


def get_item(item_id: int, user_id: int | None = None) -> sqlite3.Row | None:
    with _get_conn() as conn:
        if user_id is not None:
            return conn.execute(
                f"SELECT {_ITEM_COLS} FROM items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        return conn.execute(
            f"SELECT {_ITEM_COLS} FROM items WHERE id = ?", (item_id,)
        ).fetchone()


def search_items(query: str, user_id: int | None = None) -> list[sqlite3.Row]:
    pattern = f"%{query}%"
    with _get_conn() as conn:
        if user_id is not None:
            return conn.execute(
                f"SELECT {_ITEM_COLS} FROM items "
                "WHERE user_id = ? AND (source LIKE ? OR content LIKE ? "
                "OR analysis LIKE ? OR user_note LIKE ?) ORDER BY id DESC",
                (user_id, pattern, pattern, pattern, pattern),
            ).fetchall()
        return conn.execute(
            f"SELECT {_ITEM_COLS} FROM items "
            "WHERE source LIKE ? OR content LIKE ? OR analysis LIKE ? "
            "OR user_note LIKE ? ORDER BY id DESC",
            (pattern, pattern, pattern, pattern),
        ).fetchall()


def delete_item(item_id: int, user_id: int | None = None) -> None:
    with _get_conn() as conn:
        if user_id is not None:
            conn.execute(
                "DELETE FROM items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            )
        else:
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
