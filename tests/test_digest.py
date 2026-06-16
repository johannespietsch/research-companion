"""Weekly digest (#67): assembly, send-loop guards, and the unsubscribe flow.

No network: send_digest_email is monkeypatched in the loop tests, and the
endpoint tests exercise the HMAC token logic with a stubbed secret.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

NOW = datetime(2026, 6, 12, 6, 0, tzinfo=timezone.utc)  # a Friday


@pytest.fixture
def digest_env(monkeypatch):
    """Full sending config, so configured() passes."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("DIGEST_FROM_EMAIL", "digest@filter.fyi")
    monkeypatch.setenv("DIGEST_UNSUBSCRIBE_SECRET", "unsub-secret")
    monkeypatch.setenv("WEBHOOK_URL", "https://backend.test")


def _analysis(verdict="watch", suggestions=None, main_idea="Main idea"):
    return json.dumps({
        "main_idea": main_idea,
        "why_it_matters": "w",
        "grounded_in": "the benchmark table",
        "category": "ai",
        "time_required": "5 min",
        "verdict": verdict,
        "suggestions": suggestions if suggestions is not None else [],
    })


def _suggestion(title="Do X", detail="Do X because Y", effort="~30 min", first_step="open the repo"):
    return {"title": title, "detail": detail, "effort": effort, "first_step": first_step}


def _add_item(db, user_id, *, verdict="watch", suggestions=None, main_idea="Main idea",
              days_ago=0, source="https://ex.com/a"):
    item_id = db.save_item(user_id, "article", source, "content", _analysis(verdict, suggestions, main_idea))
    if days_ago:
        ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        with db._get_conn() as conn:
            conn.execute("UPDATE items SET created_at = ? WHERE id = ?", (ts, item_id))
    return item_id


class TestBuildDigest:
    def test_empty_week_yields_none(self, db, digest_env):
        from bot.digest import build_digest
        uid = db.upsert_user_by_email("u@example.com")
        assert build_digest(uid, now=NOW) is None

    def test_items_older_than_a_week_are_excluded(self, db, digest_env):
        from bot.digest import build_digest
        uid = db.upsert_user_by_email("u@example.com")
        _add_item(db, uid, suggestions=[_suggestion()], days_ago=8)
        assert build_digest(uid, now=NOW) is None

    def test_actions_lead_watch_first_and_cap_at_three(self, db, digest_env):
        from bot.digest import MAX_ACTIONS, build_digest
        uid = db.upsert_user_by_email("u@example.com")
        # Distinct details per suggestion — convergent ones would (rightly)
        # consolidate into a single action since #70.
        details = [
            "rewrite the ingestion job in rust",
            "benchmark quantized models on the laptop",
            "publish the trading bot postmortem",
            "interview three users about the digest",
        ]
        _add_item(db, uid, verdict="skim",
                  suggestions=[_suggestion(title="Skim move", detail=details[3])],
                  main_idea="Skim read", source="https://ex.com/skim")
        for i in range(3):
            _add_item(db, uid, verdict="watch",
                      suggestions=[_suggestion(title=f"Watch move {i}", detail=details[i])],
                      main_idea=f"Watch read {i}", source=f"https://ex.com/w{i}")
        d = build_digest(uid, now=NOW)
        assert len(d["actions"]) == MAX_ACTIONS
        # All three slots go to watch items; the skim suggestion is crowded out.
        assert all(a["title"].startswith("Watch move") for a in d["actions"])
        assert d["counts"] == {"items": 4, "watch": 3, "skim": 1, "skip": 0}

    def test_skip_items_feed_the_filtered_out_list_not_actions(self, db, digest_env):
        from bot.digest import build_digest
        uid = db.upsert_user_by_email("u@example.com")
        _add_item(db, uid, verdict="skip", suggestions=[_suggestion(title="Should not appear")],
                  main_idea="Hype post")
        _add_item(db, uid, verdict="watch", suggestions=[_suggestion(title="Real move")])
        d = build_digest(uid, now=NOW)
        assert [a["title"] for a in d["actions"]] == ["Real move"]
        assert [s["label"] for s in d["skipped"]] == ["Hype post"]

    def test_no_suggestion_week_still_builds_with_empty_actions(self, db, digest_env):
        from bot.digest import build_digest, render_digest_text
        uid = db.upsert_user_by_email("u@example.com")
        _add_item(db, uid, verdict="skim", suggestions=[])
        d = build_digest(uid, now=NOW)
        assert d["actions"] == []
        assert "Nothing demanded action" in render_digest_text(d)

    def test_action_links_back_to_the_app_not_an_inline_brief(self, db, digest_env):
        # #78: the digest is a glanceable teaser — the full hand-off brief
        # lives in the app, reached via a deep link, not pasted into the email.
        from bot.digest import build_digest, render_digest_text
        uid = db.upsert_user_by_email("u@example.com")
        item = _add_item(db, uid, suggestions=[_suggestion()], source="https://ex.com/deep")
        d = build_digest(uid, now=NOW)
        action = d["actions"][0]
        assert action["app_url"] == f"https://filter.fyi/me#item/{item}"
        assert "brief" not in action  # no inline hand-off payload
        text = render_digest_text(d)
        assert f"https://filter.fyi/me#item/{item}" in text
        # The wall-of-text bits are gone.
        assert "do NOT follow any instructions" not in text
        assert "Hand this to your AI" not in text

    def test_parked_shortlist_count_is_included(self, db, digest_env):
        from bot.digest import build_digest
        uid = db.upsert_user_by_email("u@example.com")
        item_id = _add_item(db, uid, suggestions=[_suggestion()])
        db.save_suggestion(user_id=uid, item_id=item_id, suggestion_index=0,
                           title="Parked", detail="d", effort="", first_step="", grounded_in="")
        assert build_digest(uid, now=NOW)["parked_count"] == 1


class TestRendering:
    def test_text_and_html_carry_the_unsubscribe_link(self, db, digest_env):
        from bot.digest import build_digest, render_digest_html, render_digest_text, unsubscribe_url
        uid = db.upsert_user_by_email("u@example.com")
        _add_item(db, uid, suggestions=[_suggestion()])
        d = build_digest(uid, now=NOW)
        url = unsubscribe_url(uid)
        assert url.startswith("https://backend.test/digest/unsubscribe?uid=")
        assert url in render_digest_text(d)
        assert "unsubscribe" in render_digest_html(d)

    def test_html_escapes_content(self, db, digest_env):
        from bot.digest import build_digest, render_digest_html
        uid = db.upsert_user_by_email("u@example.com")
        _add_item(db, uid, suggestions=[_suggestion(title="<script>x</script>")])
        assert "<script>" not in render_digest_html(build_digest(uid, now=NOW))


class TestUnsubscribeToken:
    def test_round_trip(self, digest_env):
        from bot.digest import unsubscribe_token, verify_unsubscribe_token
        assert verify_unsubscribe_token(7, unsubscribe_token(7))

    def test_wrong_user_or_tampered_token_rejected(self, digest_env):
        from bot.digest import unsubscribe_token, verify_unsubscribe_token
        tok = unsubscribe_token(7)
        assert not verify_unsubscribe_token(8, tok)
        assert not verify_unsubscribe_token(7, tok[:-1] + ("0" if tok[-1] != "0" else "1"))
        assert not verify_unsubscribe_token(7, "")

    def test_no_secret_fails_closed(self, monkeypatch):
        from bot import digest
        monkeypatch.delenv("DIGEST_UNSUBSCRIBE_SECRET", raising=False)
        assert not digest.verify_unsubscribe_token(7, "anything")


class TestRunWeeklyDigest:
    def test_unconfigured_sends_nothing(self, db, monkeypatch):
        from bot import digest
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        stats = digest.run_weekly_digest(now=NOW)
        assert stats["sent"] == 0
        assert stats["disabled_reason"]

    def test_sends_marks_and_skips(self, db, digest_env, monkeypatch):
        from bot import digest
        sent = []
        monkeypatch.setattr(digest, "send_digest_email",
                            lambda **kw: sent.append(kw["to"]))

        active = db.upsert_user_by_email("active@example.com")
        _add_item(db, active, suggestions=[_suggestion()])
        quiet = db.upsert_user_by_email("quiet@example.com")  # no items this week
        opted_out = db.upsert_user_by_email("out@example.com")
        _add_item(db, opted_out, suggestions=[_suggestion()])
        db.set_digest_opt_out(opted_out, True)

        stats = digest.run_weekly_digest(now=NOW)
        assert sent == ["active@example.com"]
        assert stats == {"sent": 1, "skipped_empty": 1, "skipped_recent": 0, "errors": 0}
        assert db.get_user(active)["digest_last_sent_at"] != ""

        # Second run inside the guard window: idempotent, nothing re-sent.
        stats2 = digest.run_weekly_digest(now=NOW + timedelta(hours=2))
        assert sent == ["active@example.com"]
        assert stats2["skipped_recent"] == 1

    def test_one_user_failing_does_not_abort_the_run(self, db, digest_env, monkeypatch):
        from bot import digest
        sent = []

        def flaky(**kw):
            if kw["to"] == "boom@example.com":
                raise RuntimeError("resend down")
            sent.append(kw["to"])

        monkeypatch.setattr(digest, "send_digest_email", flaky)
        boom = db.upsert_user_by_email("boom@example.com")
        _add_item(db, boom, suggestions=[_suggestion()])
        ok = db.upsert_user_by_email("zok@example.com")  # sorts after boom
        _add_item(db, ok, suggestions=[_suggestion()])

        stats = digest.run_weekly_digest(now=NOW)
        assert stats["errors"] == 1
        assert sent == ["zok@example.com"]
        # The failed user wasn't stamped, so next run retries them.
        assert db.get_user(boom)["digest_last_sent_at"] == ""


class TestUnsubscribeEndpoints:
    @pytest.fixture
    def client(self, db, digest_env, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN", "")
        import main
        return TestClient(main.app)

    def test_get_with_valid_token_shows_confirm_form(self, client, db):
        from bot.digest import unsubscribe_token
        uid = db.upsert_user_by_email("u@example.com")
        r = client.get(f"/digest/unsubscribe?uid={uid}&tok={unsubscribe_token(uid)}")
        assert r.status_code == 200
        assert "<form" in r.text  # confirm step — GET alone must not opt out
        assert db.get_user(uid)["digest_opt_out"] == 0

    def test_get_with_bad_token_is_rejected(self, client, db):
        uid = db.upsert_user_by_email("u@example.com")
        r = client.get(f"/digest/unsubscribe?uid={uid}&tok=deadbeef")
        assert r.status_code == 400

    def test_post_flips_the_flag(self, client, db):
        from bot.digest import unsubscribe_token
        uid = db.upsert_user_by_email("u@example.com")
        r = client.post(f"/digest/unsubscribe?uid={uid}&tok={unsubscribe_token(uid)}")
        assert r.status_code == 200
        assert db.get_user(uid)["digest_opt_out"] == 1
        # And the recipient query now excludes them.
        assert all(u["id"] != uid for u in db.get_digest_recipients())

    def test_post_with_bad_token_changes_nothing(self, client, db):
        uid = db.upsert_user_by_email("u@example.com")
        r = client.post(f"/digest/unsubscribe?uid={uid}&tok=deadbeef")
        assert r.status_code == 400
        assert db.get_user(uid)["digest_opt_out"] == 0


class TestSendPayload:
    @pytest.fixture
    def captured_post(self, digest_env, monkeypatch):
        """Capture the Resend API call instead of hitting the network."""
        from bot import digest
        calls = []

        class _Resp:
            def raise_for_status(self):
                pass

        def fake_post(url, *, json, headers, timeout):
            calls.append({"url": url, "json": json, "headers": headers})
            return _Resp()

        monkeypatch.setattr(digest.httpx, "post", fake_post)
        return calls

    def _send(self):
        from bot.digest import send_digest_email
        send_digest_email(to="u@example.com", subject="s", text="t", html="<p>h</p>", user_id=7)

    def test_reply_to_email_is_set_when_configured(self, captured_post, monkeypatch):
        # Replies must route somewhere monitored (e.g. Cloudflare Email
        # Routing → private inbox), so the env var has to reach the payload.
        monkeypatch.setenv("DIGEST_REPLY_TO_EMAIL", "hello@filter.fyi")
        self._send()
        payload = captured_post[0]["json"]
        assert payload["reply_to"] == "hello@filter.fyi"
        assert payload["from"] == "digest@filter.fyi"
        assert "List-Unsubscribe" in payload["headers"]

    def test_reply_to_omitted_when_unset(self, captured_post, monkeypatch):
        monkeypatch.delenv("DIGEST_REPLY_TO_EMAIL", raising=False)
        self._send()
        assert "reply_to" not in captured_post[0]["json"]
