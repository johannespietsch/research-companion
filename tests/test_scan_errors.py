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


def test_scrub_collapses_ids_and_url_parens():
    out = scan_errors._scrub("poll failed for subscription 9 (https://x.test/f?id=1)")
    assert out == "poll failed for subscription <n> (<url>)"


def test_comment_collapses_per_subscription_observations(monkeypatch):
    posted = {}
    monkeypatch.setattr(scan_errors.httpx, "get", lambda *a, **k: MagicMock(status_code=200, json=lambda: []))
    monkeypatch.setattr(scan_errors.httpx, "post",
                        lambda url, headers, json, timeout: posted.update(json) or MagicMock(status_code=201))
    items = [({"fingerprint": f"f{i}", "logger": "l", "level": "WARNING", "count": 2,
               "first_ts": "a", "last_ts": "b", "messages": [f"poll failed for subscription {i}"]},
              f"Subscription {i} returning 404") for i in range(9)]
    assert scan_errors._gh_comment_recurrence(129, items, "tok")
    body = posted["body"]
    assert body.count("returning 404") == 1
    assert "**Occurrences (last 24h):** 18" in body
