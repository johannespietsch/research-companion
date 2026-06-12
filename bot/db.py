import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
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

# Short-lived codes a logged-in web user generates to link their Telegram
# account. The Telegram bot redeems the code via /link <code>, which stitches
# the chat_id onto the existing web users row. One outstanding code per user.
_CREATE_LINK_CODES_SQL = """\
CREATE TABLE IF NOT EXISTS link_codes (
    code       TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
)"""

# Cache of fetched URL → extracted text + metadata. Keyed by exact URL.
# Lets us skip repeated upstream calls (which is the main vector for the
# YouTube 429s we see in prod) and makes user retries instant.
_CREATE_URL_CACHE_SQL = """\
CREATE TABLE IF NOT EXISTS url_cache (
    url        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)"""

# WARNING+ log records captured by the SQLiteLogHandler. The scan_errors
# script reads from here daily, groups by fingerprint, and files GH issues
# for unhandled bugs. `fingerprint` is a stable hash of logger + a normalized
# message — exact tracebacks differ across invocations, but the bug signature
# stays the same.
_CREATE_ERROR_LOG_SQL = """\
CREATE TABLE IF NOT EXISTS error_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    logger      TEXT NOT NULL DEFAULT '',
    level       TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    traceback   TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT ''
)"""

# Append-only signal log: how users react to verdicts/experiments. Powers the
# personalization loop (and the highest-signal "tried it / didn't" feedback).
# One row per event — multiple events per item are expected and wanted (we keep
# the timestamps to learn how quickly people act).
_CREATE_FEEDBACK_SQL = """\
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    item_id    INTEGER NOT NULL,
    signal     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (item_id) REFERENCES items(id)
)"""

# Suggestions a user parked for later — the "Shortlist". Unlike the append-only
# feedback log, this is durable, user-owned state: one row per saved suggestion,
# carrying a snapshot of the suggestion text (suggestions are regenerated per
# analysis and aren't stably keyed, so we snapshot rather than re-derive) plus
# the item_id it came from for the back-link. `status` advances saved → tried →
# done as the user follows up. UNIQUE(user, item, index) makes "save" idempotent.
_CREATE_SAVED_SUGGESTIONS_SQL = """\
CREATE TABLE IF NOT EXISTS saved_suggestions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    item_id          INTEGER NOT NULL,
    suggestion_index INTEGER NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    detail           TEXT NOT NULL DEFAULT '',
    effort           TEXT NOT NULL DEFAULT '',
    first_step       TEXT NOT NULL DEFAULT '',
    grounded_in      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'saved',
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(user_id, item_id, suggestion_index),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (item_id) REFERENCES items(id)
)"""

# Statuses a shortlisted suggestion can hold. 'tried'/'done' mirror the
# FEEDBACK_SIGNALS taps so the follow-up reads consistently across surfaces.
SAVED_SUGGESTION_STATUSES = frozenset({"saved", "tried", "done"})

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_link_codes_expires ON link_codes(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_url_cache_fetched_at ON url_cache(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_error_log_ts ON error_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_error_log_fingerprint ON error_log(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_item ON feedback(item_id)",
    "CREATE INDEX IF NOT EXISTS idx_saved_suggestions_user ON saved_suggestions(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_saved_suggestions_item ON saved_suggestions(item_id)",
    "CREATE INDEX IF NOT EXISTS idx_suggestion_signals_user ON suggestion_signals(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_subscription_items_sub ON subscription_items(subscription_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_user ON llm_calls(user_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_anon ON llm_calls(anon_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_analyze_traces_ts ON analyze_traces(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_analyze_traces_retention ON analyze_traces(retention_until)",
    "CREATE INDEX IF NOT EXISTS idx_processed_updates_ts ON processed_updates(ts)",
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_created_at ON llm_cache(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_hits_ts ON llm_cache_hits(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_hits_purpose ON llm_cache_hits(purpose, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_processed_urls_ts ON processed_urls(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_processed_urls_user ON processed_urls(user_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_processed_urls_status ON processed_urls(status, ts DESC)",
]

# Per-call LLM usage log. One row per upstream API call (analyze, summary,
# image). `user_id` is NULL for anonymous /api/try; `anon_id` carries the
# Worker's anon UUID so anon spend can be grouped per visitor and joined back
# to D1's `anon_summaries` if needed. `source_type` is denormalised so a
# "tokens by source_type" tile is one query without joining items. `status`
# distinguishes successful calls from failed ones so failure rate and spend
# share the same table.
_CREATE_LLM_CALLS_SQL = """\
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    user_id       INTEGER,
    anon_id       TEXT,
    job_id        TEXT,
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    purpose       TEXT    NOT NULL DEFAULT '',
    source_type   TEXT    NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'ok',
    error         TEXT    NOT NULL DEFAULT ''
)"""

# Short-lived job records for the async analysis flow. The Worker starts a job
# and returns immediately; the browser polls until status → 'done' or 'error'.
_CREATE_JOBS_SQL = """\
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'pending',
    result     TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)"""

# Retain terminal jobs for 1 hour — long enough for any in-flight poll to
# retrieve its result. Pending jobs stay until they resolve or the server
# restarts (the frontend times out its own polling after ~2 min).
JOB_RETENTION_SECONDS = 3_600

# Captured I/O for `purpose='analyze'` calls. Kept separate from `llm_calls`
# so the retention policy and access controls for raw content are explicit
# (llm_calls is metadata-only and can be kept forever; this table holds the
# actual user input and structured model output and is purged on a fixed TTL).
# Population is gated by ANALYZE_TRACE_CAPTURE=1 — off by default so prod
# doesn't start retaining content without an explicit operator decision.
# `retention_until` is set per row so changing the TTL doesn't retroactively
# extend rows already written; the purge helper reads the column directly.
_CREATE_ANALYZE_TRACES_SQL = """\
CREATE TABLE IF NOT EXISTS analyze_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    user_id         INTEGER,
    anon_id         TEXT,
    job_id          TEXT,
    provider        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL DEFAULT '',
    source_type     TEXT    NOT NULL DEFAULT '',
    input_text      TEXT    NOT NULL DEFAULT '',
    profile_text    TEXT    NOT NULL DEFAULT '',
    output_json     TEXT    NOT NULL DEFAULT '',
    retention_until TEXT    NOT NULL
)"""

ANALYZE_TRACE_RETENTION_SECONDS = int(
    os.getenv("ANALYZE_TRACE_RETENTION_SECONDS") or 30 * 24 * 3_600
)

# Telegram update-id dedup ledger. The webhook claims an `update_id` before
# scheduling any background work; a second arrival of the same id (a
# Telegram retry) finds the row already present and is dropped. This is the
# belt-and-braces follow-up to the fire-and-forget webhook fix in #34 — even
# if the handler chain ever blocks again, retries can't double-process.
# Retention is short — Telegram's active retry window is minutes, but we keep
# rows for a generous interval so a delayed retry still dedups cleanly.
_CREATE_PROCESSED_UPDATES_SQL = """\
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id  INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)"""

PROCESSED_UPDATES_RETENTION_SECONDS = 24 * 3_600  # 24h is well past any plausible retry

# Content-addressed cache of LLM results (analyze + summary). Keyed by a hash
# of everything that determines the output (provider, model, prompt template,
# schema where applicable, profile text for analyze, input content). Any
# change to those inputs auto-invalidates because the key changes. Stops the
# same content from costing tokens twice — e.g. user retries, Telegram
# delivers a duplicate, or the prompt is identical across two users with the
# same profile.
_CREATE_LLM_CACHE_SQL = """\
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key   TEXT PRIMARY KEY,
    purpose     TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
)"""

LLM_CACHE_TTL_SECONDS = 30 * 24 * 3_600  # 30d — disk bound, not correctness

# Hit log for the cache above. One row per cache hit so the admin dashboard
# can show a "calls saved / cost avoided" tile alongside the regular spend
# numbers. The actual cached payload lives in `llm_cache`; this table is
# pure event log. Cost is captured at hit time (estimated from recent
# average cost-per-call for the same purpose) so the savings number stays
# honest even after a model/price change.
_CREATE_LLM_CACHE_HITS_SQL = """\
CREATE TABLE IF NOT EXISTS llm_cache_hits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    purpose       TEXT    NOT NULL DEFAULT '',
    user_id       INTEGER,
    anon_id       TEXT,
    source_type   TEXT    NOT NULL DEFAULT '',
    cost_saved_usd REAL   NOT NULL DEFAULT 0
)"""

# Same retention as the cache itself — keep just long enough to show on the
# dashboard's longest window.
LLM_CACHE_HITS_RETENTION_SECONDS = 30 * 24 * 3_600

# Audit log of every URL that went through `bot.pipeline.analyze_url`.
# Populated on success and on failure — the failure rows let the dashboard
# show "URLs we tried, why we couldn't" without joining `error_log`. The
# `transcript_source` column captures whether a video used the YouTube-
# provided captions vs Whisper vs description fallback, which is the main
# driver of cost variability on video traffic. Metadata-only — full content
# stays in `items` / `llm_cache`.
_CREATE_PROCESSED_URLS_SQL = """\
CREATE TABLE IF NOT EXISTS processed_urls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    url                TEXT    NOT NULL,
    title              TEXT    NOT NULL DEFAULT '',
    source_type        TEXT    NOT NULL DEFAULT '',
    user_id            INTEGER,
    anon_id            TEXT,
    job_id             TEXT,
    status             TEXT    NOT NULL DEFAULT 'ok',
    error_code         TEXT    NOT NULL DEFAULT '',
    transcript_source  TEXT    NOT NULL DEFAULT '',
    latency_ms         INTEGER NOT NULL DEFAULT 0
)"""

# 90 days — longer than other audit tables because this one drives the
# Usage pillar's headline counts and we want a quarterly-ish window
# available without re-collecting.
PROCESSED_URLS_RETENTION_SECONDS = 90 * 24 * 3_600

# Suggestion-level interaction signals for signed-in users, forwarded by the
# Worker (the canonical event stream incl. anonymous traffic stays in D1 —
# this copy exists so the analyzer can learn from dismiss reasons and
# tried/done outcomes; see bot/signals.py). Mirrors the Worker's
# SUGGESTION_EVENTS vocabulary.
_CREATE_SUGGESTION_SIGNALS_SQL = """\
CREATE TABLE IF NOT EXISTS suggestion_signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    url              TEXT NOT NULL DEFAULT '',
    event            TEXT NOT NULL,
    suggestion_index INTEGER,
    suggestion_text  TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
)"""

SUGGESTION_SIGNAL_EVENTS = frozenset({
    "shown", "open", "copy", "open_chatgpt", "open_claude", "dismiss",
    "save", "tried", "done",
})

# Long enough to learn stable preferences, short enough to forget stale ones
# (and to bound disk growth — pruned daily with the other audit tables).
SUGGESTION_SIGNALS_RETENTION_SECONDS = 180 * 24 * 3_600

# Channel monitoring (#68): the feeds a user follows. `feed_url` is the
# canonical RSS/Atom URL (YouTube channels resolve to their Atom feed at
# subscribe time, so the poller only ever speaks one protocol). One row per
# (user, feed) — two users following the same feed each get their own row,
# because analysis is lens-personalized per user (the shared *fetch* is
# deduped by url_cache, not here).
_CREATE_SUBSCRIPTIONS_SQL = """\
CREATE TABLE IF NOT EXISTS subscriptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    feed_url       TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    source_kind    TEXT NOT NULL DEFAULT 'rss',
    last_polled_at TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(user_id, feed_url),
    FOREIGN KEY (user_id) REFERENCES users(id)
)"""

# Entries the poller has seen per subscription — the cross-poll dedupe set and
# the per-entry processing state. `status`: 'seeded' (present when the user
# subscribed; never analyzed — subscribing must not trigger a backfill of LLM
# calls), 'pending' (queued), 'done' (analyzed; item_id links the library row),
# 'error' (pipeline failed; not retried).
_CREATE_SUBSCRIPTION_ITEMS_SQL = """\
CREATE TABLE IF NOT EXISTS subscription_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    entry_url       TEXT NOT NULL,
    entry_title     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    item_id         INTEGER,
    verdict         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(subscription_id, entry_url),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
)"""

SUBSCRIPTION_ITEM_STATUSES = frozenset({"seeded", "pending", "done", "error"})

# Cap per user to bound polling load and LLM spend; generous for one person.
MAX_SUBSCRIPTIONS_PER_USER = 30
# Old seen-entries are safe to forget once they've long scrolled out of the
# feed window (feeds carry ~15–50 recent entries) — a year is comfortably past
# that, and keeps the dedupe set from growing without bound.
SUBSCRIPTION_ITEMS_RETENTION_SECONDS = 365 * 24 * 3_600

# Allowed feedback signals. Explicit taps + cheap implicit proxies. Kept as an
# allowlist so the data stays clean enough to learn from.
FEEDBACK_SIGNALS = frozenset({
    "tried",        # explicit: acted on the suggestion
    "not_for_me",   # explicit: not relevant / won't do it
    "done",         # explicit: completed it
    "opened",       # implicit: clicked through to the source
    "revisited",    # implicit: came back to the item later
})

LINK_CODE_TTL_SECONDS = 600
URL_CACHE_TTL_SECONDS = 30 * 24 * 3600


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Idempotently add a column to an existing table (CREATE TABLE IF NOT
    EXISTS won't alter a table that already exists on the Fly disk)."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


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
        conn.execute(_CREATE_LINK_CODES_SQL)
        conn.execute(_CREATE_URL_CACHE_SQL)
        conn.execute(_CREATE_ERROR_LOG_SQL)
        conn.execute(_CREATE_FEEDBACK_SQL)
        conn.execute(_CREATE_SAVED_SUGGESTIONS_SQL)
        conn.execute(_CREATE_JOBS_SQL)
        # Added after jobs shipped: carries the user-facing failure message so
        # the Worker can show *why* a job failed instead of a generic string.
        _ensure_column(conn, "jobs", "message", "message TEXT NOT NULL DEFAULT ''")
        # Weekly digest (#67): per-user opt-out + last-send stamp. The stamp
        # makes the Friday send idempotent across process restarts — a redeploy
        # right after the send must not re-mail everyone.
        _ensure_column(conn, "users", "digest_opt_out",
                       "digest_opt_out INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "digest_last_sent_at",
                       "digest_last_sent_at TEXT NOT NULL DEFAULT ''")
        conn.execute(_CREATE_LLM_CALLS_SQL)
        conn.execute(_CREATE_ANALYZE_TRACES_SQL)
        conn.execute(_CREATE_PROCESSED_UPDATES_SQL)
        conn.execute(_CREATE_LLM_CACHE_SQL)
        conn.execute(_CREATE_LLM_CACHE_HITS_SQL)
        conn.execute(_CREATE_PROCESSED_URLS_SQL)
        conn.execute(_CREATE_SUGGESTION_SIGNALS_SQL)
        conn.execute(_CREATE_SUBSCRIPTIONS_SQL)
        conn.execute(_CREATE_SUBSCRIPTION_ITEMS_SQL)
        for stmt in _CREATE_INDEXES_SQL:
            conn.execute(stmt)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


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
            "SELECT id, telegram_chat_id, email, api_token, profile, created_at, "
            "digest_opt_out, digest_last_sent_at "
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


def get_digest_recipients() -> list[sqlite3.Row]:
    """Users eligible for the weekly digest: have an email, not opted out."""
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, email, digest_last_sent_at FROM users "
            "WHERE email IS NOT NULL AND email != '' AND digest_opt_out = 0 "
            "ORDER BY id"
        ).fetchall()


def set_digest_opt_out(user_id: int, opt_out: bool) -> bool:
    """Flip the digest opt-out flag. Returns False for unknown user ids."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET digest_opt_out = ? WHERE id = ?",
            (1 if opt_out else 0, user_id),
        )
        return cur.rowcount > 0


def mark_digest_sent(user_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET digest_last_sent_at = ? WHERE id = ?",
            (_utcnow_iso(), user_id),
        )


def delete_user(user_id: int) -> dict:
    """Hard-delete a user and everything they own (GDPR erasure).

    Removes items and link codes, then the user row, in one transaction.
    Returns the deleted items' non-empty `file_path`s so the caller can unlink
    them from disk, plus row counts for erasure logging.
    """
    with _get_conn() as conn:
        file_paths = [
            r["file_path"]
            for r in conn.execute(
                "SELECT file_path FROM items WHERE user_id = ? AND file_path != ''",
                (user_id,),
            ).fetchall()
        ]
        items_deleted = conn.execute(
            "DELETE FROM items WHERE user_id = ?", (user_id,)
        ).rowcount
        conn.execute("DELETE FROM link_codes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM feedback WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM saved_suggestions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM suggestion_signals WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM subscription_items WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        user_deleted = conn.execute(
            "DELETE FROM users WHERE id = ?", (user_id,)
        ).rowcount
    return {
        "items_deleted": items_deleted,
        "user_deleted": user_deleted,
        "file_paths": file_paths,
    }


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
) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO items (user_id, source_type, source, content, "
            "analysis, user_note, file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, source_type, source, content, analysis, user_note, file_path),
        )
        return cur.lastrowid


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


def get_items_since(user_id: int, since_iso: str) -> list[sqlite3.Row]:
    """A user's items created at/after ``since_iso`` (ISO UTC), oldest first.
    Drives the weekly digest window."""
    with _get_conn() as conn:
        return conn.execute(
            f"SELECT {_ITEM_COLS} FROM items "
            "WHERE user_id = ? AND created_at >= ? ORDER BY id",
            (user_id, since_iso),
        ).fetchall()


def count_saved_suggestions_pending(user_id: int) -> int:
    """How many shortlisted suggestions are still parked (status='saved')."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM saved_suggestions "
            "WHERE user_id = ? AND status = 'saved'",
            (user_id,),
        ).fetchone()
        return int(row["n"])


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
        # Drop any feedback / shortlist rows that referenced this item (no orphans).
        conn.execute("DELETE FROM feedback WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM saved_suggestions WHERE item_id = ?", (item_id,))


def record_feedback(user_id: int, item_id: int, signal: str) -> None:
    """Append one feedback/signal event for a user's item."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (user_id, item_id, signal) VALUES (?, ?, ?)",
            (user_id, item_id, signal),
        )


# ---------------------------------------------------------------------------
# Shortlist (saved suggestions)
# ---------------------------------------------------------------------------

_SAVED_SUGGESTION_COLS = (
    "id, item_id, suggestion_index, title, detail, effort, first_step, "
    "grounded_in, status, created_at, updated_at"
)


def save_suggestion(
    user_id: int,
    item_id: int,
    suggestion_index: int,
    *,
    title: str = "",
    detail: str = "",
    effort: str = "",
    first_step: str = "",
    grounded_in: str = "",
) -> int:
    """Add (or refresh) a shortlisted suggestion. Idempotent on
    (user_id, item_id, suggestion_index): saving the same suggestion again
    refreshes its snapshot + updated_at and preserves its current status,
    rather than creating a duplicate row. Returns the row id."""
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM saved_suggestions "
            "WHERE user_id = ? AND item_id = ? AND suggestion_index = ?",
            (user_id, item_id, suggestion_index),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE saved_suggestions SET title = ?, detail = ?, effort = ?, "
                "first_step = ?, grounded_in = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
                (title, detail, effort, first_step, grounded_in, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO saved_suggestions (user_id, item_id, suggestion_index, "
            "title, detail, effort, first_step, grounded_in) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, item_id, suggestion_index, title, detail, effort,
             first_step, grounded_in),
        )
        return cur.lastrowid


def get_saved_suggestions(user_id: int) -> list[sqlite3.Row]:
    """All of a user's shortlisted suggestions, newest first. Joins the source
    item so the caller can render a back-link (source URL + the item's title,
    which lives inside the item's analysis JSON)."""
    cols = ", ".join(f"s.{c}" for c in _SAVED_SUGGESTION_COLS.replace(" ", "").split(","))
    with _get_conn() as conn:
        return conn.execute(
            f"SELECT {cols}, "
            "       i.source AS source, i.analysis AS item_analysis "
            "FROM saved_suggestions s JOIN items i ON i.id = s.item_id "
            "WHERE s.user_id = ? ORDER BY s.created_at DESC, s.id DESC",
            (user_id,),
        ).fetchall()


def update_saved_suggestion_status(saved_id: int, user_id: int, status: str) -> bool:
    """Advance a shortlisted suggestion's status. Ownership-scoped — returns
    False if the row doesn't exist or belongs to another user."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE saved_suggestions SET status = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') "
            "WHERE id = ? AND user_id = ?",
            (status, saved_id, user_id),
        )
        return cur.rowcount > 0


def delete_saved_suggestion(saved_id: int, user_id: int) -> bool:
    """Remove one shortlisted suggestion. Ownership-scoped — returns False if
    the row doesn't exist or belongs to another user."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM saved_suggestions WHERE id = ? AND user_id = ?",
            (saved_id, user_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Suggestion signals (forwarded by the Worker for signed-in users)
# ---------------------------------------------------------------------------

def record_suggestion_signal(
    user_id: int,
    event: str,
    *,
    url: str = "",
    suggestion_index: int | None = None,
    suggestion_text: str = "",
    reason: str = "",
) -> int:
    """Store one suggestion interaction event. Caller validates the event
    against SUGGESTION_SIGNAL_EVENTS; values are clipped here so a misbehaving
    caller can't bloat the table."""
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO suggestion_signals "
            "(user_id, url, event, suggestion_index, suggestion_text, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, (url or "")[:2048], event, suggestion_index,
             (suggestion_text or "")[:2048], (reason or "")[:2048]),
        )
        return cur.lastrowid


def get_suggestion_signals(
    user_id: int,
    *,
    events: tuple[str, ...] | None = None,
    before_iso: str | None = None,
    since_iso: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """A user's signals, newest first. ``before_iso``/``since_iso`` bound the
    window (the signal digest uses ``before`` for day-stable cache keys)."""
    q = ("SELECT id, url, event, suggestion_index, suggestion_text, reason, "
         "created_at FROM suggestion_signals WHERE user_id = ?")
    args: list = [user_id]
    if events:
        q += f" AND event IN ({','.join('?' * len(events))})"
        args.extend(events)
    if before_iso:
        q += " AND created_at < ?"
        args.append(before_iso)
    if since_iso:
        q += " AND created_at >= ?"
        args.append(since_iso)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _get_conn() as conn:
        return conn.execute(q, args).fetchall()


# ---------------------------------------------------------------------------
# Subscriptions (channel monitoring, #68)
# ---------------------------------------------------------------------------

def add_subscription(user_id: int, feed_url: str, *, title: str = "",
                     source_kind: str = "rss") -> int | None:
    """Create one subscription. Returns its id, or None when it already exists
    (idempotent). Raises ValueError at the per-user cap so the API can surface
    a clear message."""
    with _get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
        if n >= MAX_SUBSCRIPTIONS_PER_USER:
            raise ValueError(f"subscription cap reached ({MAX_SUBSCRIPTIONS_PER_USER})")
        cur = conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, feed_url, title, source_kind) "
            "VALUES (?, ?, ?, ?)",
            (user_id, feed_url[:2048], title[:300], source_kind),
        )
        return cur.lastrowid if cur.rowcount else None


def get_subscriptions(user_id: int) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, feed_url, title, source_kind, last_polled_at, created_at "
            "FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()


def get_all_subscriptions() -> list[sqlite3.Row]:
    """Every subscription across users — the poller's worklist."""
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, user_id, feed_url, title, source_kind, last_polled_at "
            "FROM subscriptions ORDER BY id"
        ).fetchall()


def delete_subscription(sub_id: int, user_id: int) -> bool:
    """Remove a subscription and its seen-entry state. Ownership-scoped."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE id = ? AND user_id = ?",
            (sub_id, user_id),
        )
        if not cur.rowcount:
            return False
        conn.execute(
            "DELETE FROM subscription_items WHERE subscription_id = ?", (sub_id,)
        )
        return True


def mark_subscription_polled(sub_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_polled_at = ? WHERE id = ?",
            (_utcnow_iso(), sub_id),
        )


def record_subscription_entry(
    sub_id: int, user_id: int, entry_url: str, entry_title: str = "",
    *, status: str = "pending",
) -> bool:
    """Register one feed entry as seen. Returns False if it was already known
    (the cross-poll dedupe)."""
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO subscription_items "
            "(subscription_id, user_id, entry_url, entry_title, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (sub_id, user_id, entry_url[:2048], (entry_title or "")[:300], status),
        )
        return cur.rowcount > 0


def get_pending_subscription_entries(sub_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, subscription_id, user_id, entry_url, entry_title "
            "FROM subscription_items WHERE subscription_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT ?",
            (sub_id, limit),
        ).fetchall()


def set_subscription_entry_result(
    entry_id: int, *, status: str, item_id: int | None = None, verdict: str = "",
) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE subscription_items SET status = ?, item_id = ?, verdict = ?, "
            "updated_at = ? WHERE id = ?",
            (status, item_id, verdict, _utcnow_iso(), entry_id),
        )


# ---------------------------------------------------------------------------
# Account linking (web ↔ Telegram)
# ---------------------------------------------------------------------------

def create_link_code(user_id: int) -> str:
    """Generate a 6-digit code the given user can read in DM to link their Telegram chat.
    Only one outstanding code per user; older codes for the same user are cleared."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=LINK_CODE_TTL_SECONDS)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    with _get_conn() as conn:
        conn.execute("DELETE FROM link_codes WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT INTO link_codes (code, user_id, expires_at) VALUES (?, ?, ?)",
            (code, user_id, expires_at),
        )
    return code


def redeem_link_code(code: str) -> int | None:
    """Look up which web users.id this code points to, single-use. Returns None if
    invalid or expired. Also opportunistically clears expired rows."""
    now = _utcnow_iso()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM link_codes WHERE code = ? AND expires_at > ?",
            (code, now),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM link_codes WHERE code = ?", (code,))
        conn.execute("DELETE FROM link_codes WHERE expires_at <= ?", (now,))
        return row["user_id"]


# ---------------------------------------------------------------------------
# URL cache
# ---------------------------------------------------------------------------

def get_cached_fetch(url: str, max_age_seconds: int = URL_CACHE_TTL_SECONDS) -> dict | None:
    """Return a previously-cached fetch result for `url`, or None if there's
    no entry or the entry is older than `max_age_seconds`."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM url_cache WHERE url = ? AND fetched_at > ?",
            (url, cutoff),
        ).fetchone()
    if not row:
        return None
    try:
        decoded = json.loads(row["payload"])
        return decoded if isinstance(decoded, dict) else None
    except json.JSONDecodeError:
        return None


def set_cached_fetch(url: str, payload: dict) -> None:
    """Store (or refresh) a successful fetch for `url`. Caller should skip
    calling this for failed fetches so retries are not poisoned by a cached
    empty result."""
    body = json.dumps(payload, ensure_ascii=False)
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO url_cache (url, payload, fetched_at) "
            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now')) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  payload = excluded.payload, fetched_at = excluded.fetched_at",
            (url, body),
        )


# ---------------------------------------------------------------------------
# Error log (populated by the SQLiteLogHandler; read by scripts/scan_errors.py)
# ---------------------------------------------------------------------------

def insert_error_log(
    *,
    logger: str,
    level: str,
    message: str,
    traceback: str,
    fingerprint: str,
) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO error_log (logger, level, message, traceback, fingerprint) "
            "VALUES (?, ?, ?, ?, ?)",
            (logger, level, message, traceback, fingerprint),
        )


def get_recent_errors(since_iso: str) -> list[sqlite3.Row]:
    """All error_log rows with ts >= since_iso, newest first."""
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, ts, logger, level, message, traceback, fingerprint "
            "FROM error_log WHERE ts >= ? ORDER BY ts DESC",
            (since_iso,),
        ).fetchall()


def prune_error_log(older_than_iso: str) -> int:
    """Delete rows older than the given timestamp. Returns rows removed."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM error_log WHERE ts < ?", (older_than_iso,))
        return cur.rowcount or 0


# How long to keep diagnostic error_log rows. The daily scanner only looks back
# 24h, so anything older is just disk.
ERROR_LOG_RETENTION_DAYS = 30


def _ts(dt) -> str:
    """Format a datetime to the storage timestamp shape (UTC, no tz suffix)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def prune_maintenance(now=None) -> dict:
    """Delete rows nothing needs to keep, to bound unbounded disk growth.

    Idempotent and safe to run repeatedly (e.g. daily). Covers:
      - error_log older than ERROR_LOG_RETENTION_DAYS
      - url_cache rows past their TTL
      - link_codes that have already expired (unredeemed)
    `now` is injectable for tests. Returns per-table delete counts.
    """
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    err_cutoff = _ts(now - timedelta(days=ERROR_LOG_RETENTION_DAYS))
    cache_cutoff = _ts(now - timedelta(seconds=URL_CACHE_TTL_SECONDS))
    now_str = _ts(now)
    jobs_cutoff = _ts(now - timedelta(seconds=JOB_RETENTION_SECONDS))
    updates_cutoff = _ts(now - timedelta(seconds=PROCESSED_UPDATES_RETENTION_SECONDS))
    llm_cache_cutoff = _ts(now - timedelta(seconds=LLM_CACHE_TTL_SECONDS))
    llm_cache_hits_cutoff = _ts(now - timedelta(seconds=LLM_CACHE_HITS_RETENTION_SECONDS))
    processed_urls_cutoff = _ts(now - timedelta(seconds=PROCESSED_URLS_RETENTION_SECONDS))
    with _get_conn() as conn:
        errors = conn.execute("DELETE FROM error_log WHERE ts < ?", (err_cutoff,)).rowcount or 0
        cache = conn.execute(
            "DELETE FROM url_cache WHERE fetched_at < ?", (cache_cutoff,)
        ).rowcount or 0
        codes = conn.execute(
            "DELETE FROM link_codes WHERE expires_at < ?", (now_str,)
        ).rowcount or 0
        # Only prune terminal states — pending jobs may still have a running task.
        jobs = conn.execute(
            "DELETE FROM jobs WHERE status != 'pending' AND updated_at < ?",
            (jobs_cutoff,),
        ).rowcount or 0
        updates = conn.execute(
            "DELETE FROM processed_updates WHERE ts < ?", (updates_cutoff,)
        ).rowcount or 0
        llm_cache = conn.execute(
            "DELETE FROM llm_cache WHERE created_at < ?", (llm_cache_cutoff,)
        ).rowcount or 0
        llm_cache_hits = conn.execute(
            "DELETE FROM llm_cache_hits WHERE ts < ?", (llm_cache_hits_cutoff,)
        ).rowcount or 0
        processed_urls = conn.execute(
            "DELETE FROM processed_urls WHERE ts < ?", (processed_urls_cutoff,)
        ).rowcount or 0
        suggestion_signals = conn.execute(
            "DELETE FROM suggestion_signals WHERE created_at < ?",
            (_ts(now - timedelta(seconds=SUGGESTION_SIGNALS_RETENTION_SECONDS)),),
        ).rowcount or 0
        subscription_items = conn.execute(
            "DELETE FROM subscription_items WHERE created_at < ?",
            (_ts(now - timedelta(seconds=SUBSCRIPTION_ITEMS_RETENTION_SECONDS)),),
        ).rowcount or 0
    return {
        "error_log": errors, "url_cache": cache, "link_codes": codes,
        "jobs": jobs, "processed_updates": updates, "llm_cache": llm_cache,
        "llm_cache_hits": llm_cache_hits, "processed_urls": processed_urls,
        "suggestion_signals": suggestion_signals,
        "subscription_items": subscription_items,
    }


# ---------------------------------------------------------------------------
# Async jobs
# ---------------------------------------------------------------------------

def create_job(job_id: str) -> None:
    with _get_conn() as conn:
        conn.execute("INSERT INTO jobs (id) VALUES (?)", (job_id,))


def set_job_done(job_id: str, result: dict) -> None:
    body = json.dumps(result, ensure_ascii=False)
    with _get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'done', result = ?, updated_at = ? WHERE id = ?",
            (body, _utcnow_iso(), job_id),
        )


def set_job_error(job_id: str, error: str, message: str = "") -> None:
    """Mark a job failed. `error` is the machine code (the Worker may key on
    it); `message` is the user-facing explanation surfaced in the browser."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'error', error = ?, message = ?, updated_at = ? WHERE id = ?",
            (error, message, _utcnow_iso(), job_id),
        )


def get_job_record(job_id: str) -> sqlite3.Row | None:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, status, result, error, message FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()


# ---------------------------------------------------------------------------
# Telegram update-id dedup
# ---------------------------------------------------------------------------

def claim_telegram_update(update_id: int) -> bool:
    """Atomically reserve a Telegram update_id for processing.

    Returns True if this is the first time we've seen this id and the caller
    should proceed with the handler chain. Returns False if it was already
    claimed (a Telegram retry, or a duplicate webhook delivery), meaning the
    caller should ack 200 but do no work.

    Implementation uses INSERT OR IGNORE so the check-and-set is a single
    atomic operation — two simultaneous arrivals of the same id can both
    pass an "exists" check but only one INSERT succeeds. SQLite reports the
    successful insert via rowcount == 1.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_updates (update_id) VALUES (?)",
            (update_id,),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# LLM result cache (content-addressed)
# ---------------------------------------------------------------------------

def get_cached_llm_result(cache_key: str) -> str | None:
    """Return the cached payload for `cache_key`, or None on miss.

    The caller knows what to do with the string (json.loads for analyze,
    return as-is for summary). Keeping the table type-agnostic means we
    can reuse the same machinery for any future LLM purpose.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM llm_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    return row["payload"] if row else None


def set_cached_llm_result(cache_key: str, purpose: str, payload: str) -> None:
    """Persist an LLM result. Uses ON CONFLICT DO UPDATE so concurrent first-
    misses (two requests with the same input racing past `get_cached_llm_result`
    returning None) settle on the later value rather than crash. Same payload
    either way — both ran the same model on the same inputs."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO llm_cache (cache_key, purpose, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "  payload = excluded.payload, "
            "  created_at = strftime('%Y-%m-%dT%H:%M:%S','now')",
            (cache_key, purpose, payload),
        )


def record_cache_hit(
    *,
    purpose: str,
    user_id: int | None = None,
    anon_id: str | None = None,
    source_type: str = "",
    cost_saved_usd: float = 0.0,
) -> None:
    """Log one cache hit. Best-effort — a DB blip here must never break the
    analyse path. Cost saved is estimated by the caller from recent average
    cost-per-call for the same purpose."""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO llm_cache_hits (purpose, user_id, anon_id, source_type, cost_saved_usd) "
                "VALUES (?, ?, ?, ?, ?)",
                (purpose, user_id, anon_id, source_type, float(cost_saved_usd)),
            )
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("record_cache_hit failed")


def estimate_avg_cost_per_call(purpose: str, lookback_days: int = 7) -> float:
    """Average cost per *successful* upstream call of this purpose in the
    recent past. Used to attach an estimated savings amount to cache hits.

    Falls back to 0 when there's no data yet — the dashboard then shows
    "calls saved" without a $ figure, which is still meaningful.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT AVG(cost_usd) AS avg_cost FROM llm_calls "
                "WHERE purpose = ? AND status = 'ok' AND ts >= ? AND cost_usd > 0",
                (purpose, since),
            ).fetchone()
        return float(row["avg_cost"] or 0.0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Processed URLs audit log
# ---------------------------------------------------------------------------

def record_processed_url(
    *,
    url: str,
    title: str = "",
    source_type: str = "",
    user_id: int | None = None,
    anon_id: str | None = None,
    job_id: str | None = None,
    status: str = "ok",
    error_code: str = "",
    transcript_source: str = "",
    latency_ms: int = 0,
) -> None:
    """Log one URL that went through the pipeline. Best-effort — a DB blip
    here must NEVER break the analyse path, the audit log is observability,
    not correctness."""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO processed_urls "
                "(url, title, source_type, user_id, anon_id, job_id, "
                "status, error_code, transcript_source, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url, title, source_type, user_id, anon_id, job_id,
                    status, error_code, transcript_source, int(latency_ms),
                ),
            )
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("record_processed_url failed")


# ---------------------------------------------------------------------------
# LLM usage log
# ---------------------------------------------------------------------------

def insert_llm_call(
    *,
    provider: str,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    status: str = "ok",
    error: str = "",
    user_id: int | None = None,
    anon_id: str | None = None,
    job_id: str | None = None,
    source_type: str = "",
) -> None:
    """Record one upstream LLM call. Never raises into the caller — usage
    logging is best-effort; failing to log must not break the analysis path."""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO llm_calls (user_id, anon_id, job_id, provider, "
                "model, purpose, source_type, input_tokens, output_tokens, "
                "cost_usd, latency_ms, status, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, anon_id, job_id, provider, model, purpose,
                    source_type, int(input_tokens), int(output_tokens),
                    float(cost_usd), int(latency_ms), status, error,
                ),
            )
    except Exception:
        # Surface in logs but never propagate — see docstring.
        import logging as _logging
        _logging.getLogger(__name__).exception("insert_llm_call failed")


# ---------------------------------------------------------------------------
# Analyze trace capture (eval dataset source)
# ---------------------------------------------------------------------------

def analyze_trace_capture_enabled() -> bool:
    """Read the env flag at call time so tests (and ops) can toggle without
    reimporting the module. Truthy = "1", "true", "yes" (case-insensitive)."""
    return (os.getenv("ANALYZE_TRACE_CAPTURE") or "").strip().lower() in {"1", "true", "yes"}


def record_analyze_trace(
    *,
    provider: str,
    model: str,
    source_type: str,
    input_text: str,
    profile_text: str,
    output: dict,
    user_id: int | None = None,
    anon_id: str | None = None,
    job_id: str | None = None,
) -> None:
    """Persist one `analyze` call's input and structured output for offline eval.

    No-op when ANALYZE_TRACE_CAPTURE is unset. Never raises into the caller —
    trace capture is best-effort and must not break the analysis path.
    """
    if not analyze_trace_capture_enabled():
        return
    try:
        retention_until = (
            datetime.now(timezone.utc) + timedelta(seconds=ANALYZE_TRACE_RETENTION_SECONDS)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO analyze_traces (user_id, anon_id, job_id, provider, "
                "model, source_type, input_text, profile_text, output_json, "
                "retention_until) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, anon_id, job_id, provider, model, source_type,
                    input_text or "", profile_text or "",
                    json.dumps(output, ensure_ascii=False), retention_until,
                ),
            )
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("record_analyze_trace failed")


def purge_expired_analyze_traces() -> int:
    """Delete trace rows whose retention_until is in the past. Returns rowcount.

    Meant to be called from a periodic task (same cadence as other cleanup).
    """
    now = _utcnow_iso()
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM analyze_traces WHERE retention_until <= ?", (now,)
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Account linking (web ↔ Telegram)
# ---------------------------------------------------------------------------

def link_telegram_to_user(web_user_id: int, telegram_chat_id: int) -> None:
    """Stitch a Telegram chat onto the given web user. Idempotent.

    If a separate `users` row already exists for `telegram_chat_id` (the typical
    case — the person has been using the bot for a while), its items are moved
    onto `web_user_id` and the orphan row is deleted. Web-side `profile` and
    `api_token` win unless they're empty, in which case the Telegram-side values
    are copied over. Raises ValueError if the web user is already linked to a
    different Telegram chat.
    """
    with _get_conn() as conn:
        web = conn.execute(
            "SELECT id, telegram_chat_id, profile, api_token FROM users WHERE id = ?",
            (web_user_id,),
        ).fetchone()
        if not web:
            raise ValueError(f"web user {web_user_id} not found")
        if web["telegram_chat_id"] is not None and web["telegram_chat_id"] != telegram_chat_id:
            raise ValueError(
                f"web user {web_user_id} is already linked to Telegram chat "
                f"{web['telegram_chat_id']}"
            )

        tg = conn.execute(
            "SELECT id, profile, api_token FROM users WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()

        if tg and tg["id"] == web_user_id:
            return  # already linked, no-op

        # Always set telegram_chat_id at the end. When a separate tg row exists,
        # capture its donatable fields, move its items, then DELETE it BEFORE
        # we copy anything onto the web row — both rows can't hold the same
        # UNIQUE value (api_token, telegram_chat_id) simultaneously.
        profile_to_copy = tg["profile"] if tg and not web["profile"] and tg["profile"] else None
        api_token_to_copy = (
            tg["api_token"] if tg and not web["api_token"] and tg["api_token"] else None
        )

        if tg:
            conn.execute(
                "UPDATE items SET user_id = ? WHERE user_id = ?",
                (web_user_id, tg["id"]),
            )
            conn.execute("DELETE FROM users WHERE id = ?", (tg["id"],))

        updates: dict[str, object] = {"telegram_chat_id": telegram_chat_id}
        if profile_to_copy is not None:
            updates["profile"] = profile_to_copy
        if api_token_to_copy is not None:
            updates["api_token"] = api_token_to_copy

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",
            list(updates.values()) + [web_user_id],
        )
