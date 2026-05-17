import os
import sqlite3
from pathlib import Path

# DATA_DIR lets us point at a mounted volume in prod (Fly) while keeping the
# default of "project root" so local dev / tests keep working unchanged.
_DATA_DIR = Path(os.getenv("DATA_DIR") or (Path(__file__).parent.parent))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DATA_DIR / "research.db"

_CREATE_ITEMS_SQL = """\
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'unknown',
    source      TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    analysis    TEXT NOT NULL DEFAULT '',
    user_note   TEXT NOT NULL DEFAULT '',
    file_path   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)"""

_CREATE_PROFILES_SQL = """\
CREATE TABLE IF NOT EXISTS profiles (
    user_id   TEXT PRIMARY KEY,
    content   TEXT NOT NULL DEFAULT '',
    email     TEXT UNIQUE,
    api_token TEXT UNIQUE
)"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init():
    with _get_conn() as conn:
        info = conn.execute("PRAGMA table_info(items)").fetchall()
        cols = {row["name"] for row in info}

        if not cols:
            conn.execute(_CREATE_ITEMS_SQL)
            conn.execute(_CREATE_PROFILES_SQL)
            return

        if "source_type" not in cols:
            # Migrate from very old schema (id, url, analysis, maybe created_at)
            has_created_at = "created_at" in cols
            conn.execute("ALTER TABLE items RENAME TO _items_old")
            conn.execute(_CREATE_ITEMS_SQL)
            ts_col = (
                "COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%S', 'now'))"
                if has_created_at
                else "strftime('%Y-%m-%dT%H:%M:%S', 'now')"
            )
            conn.execute(
                f"INSERT INTO items (id, source, analysis, created_at) "
                f"SELECT id, COALESCE(url, ''), COALESCE(analysis, ''), {ts_col} "
                f"FROM _items_old"
            )
            conn.execute("DROP TABLE _items_old")
            # Re-fetch cols after migration
            info = conn.execute("PRAGMA table_info(items)").fetchall()
            cols = {row["name"] for row in info}

        # Add user_id column if missing (migration from single-user schema)
        if "user_id" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")

        # Add file_path column if missing
        if "file_path" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN file_path TEXT NOT NULL DEFAULT ''")

        conn.execute(_CREATE_PROFILES_SQL)

        # Migrate profiles table: add email + api_token if missing
        profile_info = conn.execute("PRAGMA table_info(profiles)").fetchall()
        profile_cols = {row["name"] for row in profile_info}
        if "email" not in profile_cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN email TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_email ON profiles(email)")
        if "api_token" not in profile_cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN api_token TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_api_token ON profiles(api_token)")


_init()


def save_item(
    user_id: str,
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
            "INSERT INTO items (user_id, source_type, source, content, analysis, user_note, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, source_type, source, content, analysis, user_note, file_path),
        )


def get_all_items(user_id: str | None = None) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        if user_id is not None:
            return conn.execute(
                "SELECT id, user_id, source_type, source, content, analysis, user_note, file_path, created_at "
                "FROM items WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        return conn.execute(
            "SELECT id, user_id, source_type, source, content, analysis, user_note, file_path, created_at "
            "FROM items ORDER BY id DESC"
        ).fetchall()


def get_item(item_id: int, user_id: str | None = None) -> sqlite3.Row | None:
    with _get_conn() as conn:
        if user_id is not None:
            return conn.execute(
                "SELECT id, user_id, source_type, source, content, analysis, user_note, file_path, created_at "
                "FROM items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        return conn.execute(
            "SELECT id, user_id, source_type, source, content, analysis, user_note, file_path, created_at "
            "FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()


def search_items(query: str, user_id: str | None = None) -> list[sqlite3.Row]:
    pattern = f"%{query}%"
    with _get_conn() as conn:
        if user_id is not None:
            return conn.execute(
                "SELECT id, user_id, source_type, source, content, analysis, user_note, file_path, created_at "
                "FROM items "
                "WHERE user_id = ? AND (source LIKE ? OR content LIKE ? OR analysis LIKE ? OR user_note LIKE ?) "
                "ORDER BY id DESC",
                (user_id, pattern, pattern, pattern, pattern),
            ).fetchall()
        return conn.execute(
            "SELECT id, user_id, source_type, source, content, analysis, user_note, file_path, created_at "
            "FROM items "
            "WHERE source LIKE ? OR content LIKE ? OR analysis LIKE ? OR user_note LIKE ? "
            "ORDER BY id DESC",
            (pattern, pattern, pattern, pattern),
        ).fetchall()


def delete_item(item_id: int, user_id: str | None = None) -> None:
    with _get_conn() as conn:
        if user_id is not None:
            conn.execute("DELETE FROM items WHERE id = ? AND user_id = ?", (item_id, user_id))
        else:
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


def get_profile(user_id: str) -> str:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT content FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["content"] if row else ""


def set_profile(user_id: str, content: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO profiles (user_id, content) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET content = excluded.content",
            (user_id, content),
        )


def set_profile_field(user_id: str, **fields) -> None:
    """Upsert arbitrary profile columns (e.g. email, api_token)."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = excluded.{k}" for k in fields)
    col_names = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    values = list(fields.values())
    with _get_conn() as conn:
        conn.execute(
            f"INSERT INTO profiles (user_id, {col_names}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {set_clause}",
            [user_id] + values,
        )


def get_profile_row(user_id: str) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT user_id, content, email, api_token FROM profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def get_profile_by_token(token: str) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT user_id, content, email, api_token FROM profiles WHERE api_token = ?",
            (token,),
        ).fetchone()


def get_profile_by_email(email: str) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT user_id, content, email, api_token FROM profiles WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()


def create_web_user(email: str, api_token: str) -> str:
    """Create a web-only user. Returns user_id. Raises ValueError if email exists."""
    email = email.lower().strip()
    user_id = f"web:{email}"
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM profiles WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            raise ValueError(f"Email {email} is already registered (user_id={existing['user_id']})")
        conn.execute(
            "INSERT INTO profiles (user_id, email, api_token) VALUES (?, ?, ?)",
            (user_id, email, api_token),
        )
    return user_id
