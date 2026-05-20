"""Capture WARNING+ records to the SQLite `error_log` table.

The handler is installed once from main.py. The companion `scripts/scan_errors.py`
reads from this table, groups by fingerprint, classifies with an LLM, and files
GitHub issues for genuine bugs.

Fingerprinting strategy: SHA1 of `(logger, normalized_message)`. We strip URLs,
chat ids, tweet ids, file paths, and digits so the same bug with different
inputs collapses to one fingerprint.
"""
from __future__ import annotations

import hashlib
import logging
import re
import traceback as tb_mod


_NORMALIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"/tmp/\S+"), "<tmp>"),
    (re.compile(r"/data/\S+"), "<data>"),
    (re.compile(r"\b[0-9a-f]{16,}\b"), "<hex>"),
    (re.compile(r"\b\d{6,}\b"), "<num>"),
    (re.compile(r"\s+"), " "),
]


def _normalize(msg: str) -> str:
    out = msg
    for pat, repl in _NORMALIZE_PATTERNS:
        out = pat.sub(repl, out)
    return out.strip().lower()


def _fingerprint(logger_name: str, message: str, exc_first_line: str) -> str:
    base = f"{logger_name}|{_normalize(message)}|{_normalize(exc_first_line)}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


class SQLiteLogHandler(logging.Handler):
    """Writes WARNING+ records to bot.db.error_log.

    Importing bot.db at handler-init time would create a circular import in
    test environments (db.py is reloaded per test). We defer the import to
    emit-time.
    """

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                tb_text = "".join(tb_mod.format_exception(*record.exc_info))
                exc_first_line = tb_mod.format_exception_only(
                    record.exc_info[0], record.exc_info[1]
                )[0].strip()
            else:
                tb_text = ""
                exc_first_line = ""

            fp = _fingerprint(record.name, message, exc_first_line)

            # Defer db import — keeps test isolation working.
            from bot import db as _db

            _db.insert_error_log(
                logger=record.name,
                level=record.levelname,
                message=message,
                traceback=tb_text,
                fingerprint=fp,
            )
        except Exception:
            # Never let a logging handler crash the app.
            self.handleError(record)


def install() -> None:
    """Attach the handler to the root logger. Safe to call multiple times."""
    root = logging.getLogger()
    if any(isinstance(h, SQLiteLogHandler) for h in root.handlers):
        return
    root.addHandler(SQLiteLogHandler())
