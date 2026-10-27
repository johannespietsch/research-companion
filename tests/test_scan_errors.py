"""scan_errors: duplicate bugs get a recurrence comment instead of a new issue."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from scripts import scan_errors


def _row(fp, msg):
    return {"fingerprint": fp, "logger": "bot.monitor", "level": "WARNING",
            "ts": "2026-10-22T04:13:51", "message": msg, "traceback": ""}


def _run(monkeypatch, classification, open_issues, rows=None):
    import bot.db as db
    rows = rows or [_row("fp1", "poll failed for subscription 6")]
    monkeypatch.setattr(db, "get_recent_errors", lambda since: rows)
    monkeypatch.setattr(db, "prune_error_log", lambda cutoff: 0)
    fake_app = types.SimpleNamespace(configured=lambda: True, installation_token=lambda repo: "tok")
    monkeypatch.setitem(sys.modules, "scripts.github_app", fake_app)
    monkeypatch.setattr("scripts.github_app", fake_app, raising=False)
    monkeypatch.setattr(scan_errors, "_gh_list_open_issues", lambda token: open_issues)
    monkeypatch.setattr(scan_errors, "_classify", lambda groups, issues=None: [dict(classification, fingerprint=g["fingerprint"]) for g in groups])
    monkeypatch.setattr(scan_errors, "_gh_search_existing", lambda fp, token: None)
    create, comment = MagicMock(return_value=999), MagicMock(return_value=True)
    monkeypatch.setattr(scan_errors, "_gh_create_issue", create)
    monkeypatch.setattr(scan_errors, "_gh_comment_recurrence", comment)
    result = scan_errors.run_scan(prune_older_than_days=0)
    return result, create, comment


def test_duplicate_of_open_issue_comments_instead_of_filing(monkeypatch):
    result, create, comment = _run(
        monkeypatch,
        {"fingerprint": "fp1", "classification": "bug", "duplicate_of": 129,
         "observation": "now a 500, subscription 6"},
        [{"number": 129, "title": "feed polls fail", "body": ""}],
    )
    create.assert_not_called()
    assert comment.call_args.args[0] == 129
    assert comment.call_args.args[1][0][1] == "now a 500, subscription 6"
    assert result["commented"] == 1 and result["filed"] == 0


def test_hallucinated_duplicate_number_files_new_issue(monkeypatch):
    result, create, comment = _run(
        monkeypatch,
        {"fingerprint": "fp1", "classification": "bug", "duplicate_of": 5000, "issue_title": "t"},
        [{"number": 129, "title": "x", "body": ""}],
    )
    create.assert_called_once()
    comment.assert_not_called()
    assert result["filed"] == 1


def test_many_groups_for_one_issue_post_a_single_comment(monkeypatch):
    rows = [_row(f"fp{i}", f"poll failed {i}") for i in range(3)]
    result, create, comment = _run(
        monkeypatch,
        {"classification": "bug", "duplicate_of": 129, "observation": "404s"},
        [{"number": 129, "title": "feed polls fail", "body": ""}],
        rows=rows,
    )
    create.assert_not_called()
    comment.assert_called_once()
    assert len(comment.call_args.args[1]) == 3
    assert result["commented"] == 1


def test_scrub_strips_urls():
    assert "youtube" not in scan_errors._scrub("poll failed (https://www.youtube.com/feeds/videos.xml?channel_id=X)")
