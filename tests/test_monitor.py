"""Channel monitoring (#68): feed resolution, the poll cycle's cost/dedupe
contracts, and the subscriptions API.

No network anywhere: monitor._get is stubbed for resolution tests, and
fetch_entries / pipeline.analyze_url are monkeypatched for the poll tests.

The contracts that matter most:
  1. Subscribing NEVER backfills — entries already in the feed are seeded as
     seen-but-not-analyzed. Only drops that arrive later cost LLM calls.
  2. Entries are analyzed once per subscription (cross-poll dedupe), with
     per-subscription and per-cycle budgets bounding spend.
  3. A saturated box (ERR_BUSY) leaves entries pending — monitoring sheds
     before interactive traffic.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>My Blog</title>
<item><link>https://ex.com/post-a</link><title>Post A</title></item>
<item><link>https://ex.com/post-b</link><title>Post B</title></item>
</channel></rss>"""

HTML_WITH_FEED = """<!doctype html><html><head>
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
</head><body>hi</body></html>"""

HTML_NO_FEED = "<!doctype html><html><head></head><body>plain page</body></html>"

YT_CHANNEL_PAGE = '<html><script>var x = {"channelId":"UCabcdefghijklmnopqrstuv"};</script></html>'


def _resp(url: str, body: str):
    return SimpleNamespace(url=url, text=body, content=body.encode())


@pytest.fixture
def fake_get(monkeypatch):
    """Route monitor._get through an in-memory URL→body table."""
    from bot import monitor
    pages: dict[str, str] = {}

    def get(url):
        if url not in pages:
            raise monitor.FeedError(f"unreachable in test: {url}")
        return _resp(url, pages[url])

    monkeypatch.setattr(monitor, "_get", get)
    return pages


class TestResolveFeed:
    def test_raw_feed_url_passes_through(self, fake_get):
        from bot.monitor import resolve_feed
        fake_get["https://ex.com/feed.xml"] = FEED_XML
        r = resolve_feed("https://ex.com/feed.xml")
        assert r == {"feed_url": "https://ex.com/feed.xml", "title": "My Blog",
                     "source_kind": "rss"}

    def test_html_page_discovers_advertised_feed(self, fake_get):
        from bot.monitor import resolve_feed
        fake_get["https://ex.com/blog"] = HTML_WITH_FEED
        fake_get["https://ex.com/feed.xml"] = FEED_XML
        r = resolve_feed("https://ex.com/blog")
        assert r["feed_url"] == "https://ex.com/feed.xml"  # relative href resolved
        assert r["source_kind"] == "rss"

    def test_youtube_channel_url_maps_to_atom_feed_without_page_fetch(self, fake_get):
        from bot.monitor import resolve_feed
        feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
        fake_get[feed] = FEED_XML
        r = resolve_feed("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")
        assert r["feed_url"] == feed
        assert r["source_kind"] == "youtube"

    def test_youtube_handle_page_yields_channel_id(self, fake_get):
        from bot.monitor import resolve_feed
        feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
        fake_get["https://www.youtube.com/@somecreator"] = YT_CHANNEL_PAGE
        fake_get[feed] = FEED_XML
        r = resolve_feed("https://www.youtube.com/@somecreator")
        assert r["feed_url"] == feed
        assert r["source_kind"] == "youtube"

    def test_youtube_channel_id_via_canonical_link_fallback(self, fake_get):
        # Markup without "channelId" but with the canonical /channel/ link —
        # the resolver must still find the id (#65 robustness).
        from bot.monitor import resolve_feed
        feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
        page = ('<html><head><link rel="canonical" '
                'href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv">'
                '</head></html>')
        fake_get["https://www.youtube.com/@handleonly"] = page
        fake_get[feed] = FEED_XML
        assert resolve_feed("https://www.youtube.com/@handleonly")["feed_url"] == feed

    def test_page_without_feed_raises_user_facing_error(self, fake_get):
        from bot.monitor import FeedError, resolve_feed
        fake_get["https://ex.com/nothing"] = HTML_NO_FEED
        with pytest.raises(FeedError, match="No feed found"):
            resolve_feed("https://ex.com/nothing")


class TestYouTubeConsentCookie:
    def test_get_sends_consent_cookie_to_youtube_only(self, monkeypatch):
        # #65: datacenter IPs hit YouTube's EU consent wall without this cookie,
        # so the channel page (and its channelId) never loads.
        from bot import monitor
        seen = {}

        class _Resp:
            def __init__(self, url):
                self.url = url
                self.content = b"<rss></rss>"
                self.text = "<rss></rss>"
            def raise_for_status(self):
                pass

        def fake_httpx_get(url, **kw):
            seen[url] = kw.get("headers", {})
            return _Resp(url)

        monkeypatch.setattr(monitor.httpx, "get", fake_httpx_get)
        monkeypatch.setattr(monitor, "assert_public_url", lambda u: None)

        monitor._get("https://www.youtube.com/@x")
        monitor._get("https://blog.example/feed.xml")
        assert "SOCS" in seen["https://www.youtube.com/@x"].get("Cookie", "")
        assert "Cookie" not in seen["https://blog.example/feed.xml"]

    def test_non_public_target_is_blocked(self):
        # Real SSRF guard, no stub: localhost is rejected before any DNS.
        from bot.monitor import resolve_feed
        from bot.ssrf import BlockedURLError
        with pytest.raises(BlockedURLError):
            resolve_feed("http://localhost/feed.xml")

    def test_scheme_is_defaulted(self, fake_get):
        from bot.monitor import resolve_feed
        fake_get["https://ex.com/feed.xml"] = FEED_XML
        assert resolve_feed("ex.com/feed.xml")["title"] == "My Blog"


class TestFetchEntries:
    def test_parses_links_and_titles(self, fake_get):
        from bot.monitor import fetch_entries
        fake_get["https://ex.com/feed.xml"] = FEED_XML
        entries = fetch_entries("https://ex.com/feed.xml")
        assert entries == [
            {"url": "https://ex.com/post-a", "title": "Post A"},
            {"url": "https://ex.com/post-b", "title": "Post B"},
        ]


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def _fake_result(saved_id=11, verdict="watch"):
    return SimpleNamespace(saved_id=saved_id, analysis={"verdict": verdict})


@pytest.fixture
def polling(db, monkeypatch):
    """A user + subscription, with the feed and the pipeline both stubbed.
    Returns a context object tests mutate (feed entries, analyze behaviour)."""
    from bot import monitor, pipeline

    ctx = SimpleNamespace(entries=[], analyzed=[], analyze_error=None, db=db)
    uid = db.upsert_user_by_email("u@example.com")
    sub_id = db.add_subscription(uid, "https://ex.com/feed.xml", title="My Blog")
    ctx.user_id, ctx.sub_id = uid, sub_id

    monkeypatch.setattr(monitor, "fetch_entries", lambda feed_url: list(ctx.entries))

    async def fake_analyze_url(url, *, ctx_=None, **kw):
        if ctx.analyze_error is not None:
            raise ctx.analyze_error
        ctx.analyzed.append(url)
        return _fake_result()

    monkeypatch.setattr(pipeline, "analyze_url", fake_analyze_url)

    def sub_row():
        return next(s for s in db.get_all_subscriptions() if s["id"] == sub_id)

    def poll():
        from bot.monitor import poll_subscription
        return asyncio.run(poll_subscription(sub_row(), analysis_budget=99))

    ctx.poll = poll
    ctx.statuses = lambda: {
        r["entry_url"]: r["status"]
        for r in db._get_conn().execute(
            "SELECT entry_url, status FROM subscription_items").fetchall()
    }
    return ctx


class TestPollSubscription:
    def test_first_poll_seeds_without_analyzing(self, polling):
        polling.entries = [{"url": "https://ex.com/old", "title": "Old"}]
        stats = polling.poll()
        assert stats == {"new": 1, "analyzed": 0, "errors": 0, "busy": False}
        assert polling.analyzed == []
        assert polling.statuses() == {"https://ex.com/old": "seeded"}

    def test_new_drop_after_seeding_is_analyzed_once(self, polling):
        polling.entries = [{"url": "https://ex.com/old", "title": "Old"}]
        polling.poll()  # seed
        polling.entries = [{"url": "https://ex.com/new", "title": "New"},
                           {"url": "https://ex.com/old", "title": "Old"}]
        stats = polling.poll()
        assert stats["analyzed"] == 1
        assert polling.analyzed == ["https://ex.com/new"]
        assert polling.statuses()["https://ex.com/new"] == "done"
        # Third poll: nothing new, nothing re-analyzed (cross-poll dedupe).
        stats = polling.poll()
        assert stats["new"] == 0 and stats["analyzed"] == 0

    def test_done_entry_links_item_and_verdict(self, polling):
        polling.entries = []
        polling.poll()  # seed (empty)
        polling.entries = [{"url": "https://ex.com/new", "title": "New"}]
        polling.poll()
        row = polling.db._get_conn().execute(
            "SELECT item_id, verdict FROM subscription_items WHERE entry_url = ?",
            ("https://ex.com/new",),
        ).fetchone()
        assert row["item_id"] == 11
        assert row["verdict"] == "watch"

    def test_busy_box_leaves_entries_pending(self, polling):
        from bot.pipeline import ERR_BUSY, PipelineError
        polling.entries = []
        polling.poll()  # seed
        polling.entries = [{"url": "https://ex.com/new", "title": "New"}]
        polling.analyze_error = PipelineError(ERR_BUSY, message="busy")
        stats = polling.poll()
        assert stats["busy"] is True
        assert polling.statuses()["https://ex.com/new"] == "pending"
        # Capacity returns → the pending entry is picked up next cycle.
        polling.analyze_error = None
        stats = polling.poll()
        assert stats["analyzed"] == 1
        assert polling.statuses()["https://ex.com/new"] == "done"

    def test_pipeline_failure_marks_error_and_does_not_retry(self, polling):
        from bot.pipeline import ERR_FETCH_FAILED, PipelineError
        polling.entries = []
        polling.poll()
        polling.entries = [{"url": "https://ex.com/broken", "title": "B"}]
        polling.analyze_error = PipelineError(ERR_FETCH_FAILED, message="nope")
        stats = polling.poll()
        assert stats["errors"] == 1
        assert polling.statuses()["https://ex.com/broken"] == "error"
        polling.analyze_error = None
        assert polling.poll()["analyzed"] == 0  # errored entries stay done-with

    def test_per_poll_cap_bounds_analysis(self, polling, monkeypatch):
        from bot import monitor
        monkeypatch.setattr(monitor, "MAX_NEW_PER_POLL", 2)
        polling.entries = []
        polling.poll()
        polling.entries = [{"url": f"https://ex.com/p{i}", "title": str(i)} for i in range(5)]
        stats = polling.poll()
        assert stats["analyzed"] == 2


class TestPollAllSubscriptions:
    def test_fan_out_and_contained_feed_failure(self, db, monkeypatch):
        from bot import monitor, pipeline

        ua = db.upsert_user_by_email("a@example.com")
        ub = db.upsert_user_by_email("b@example.com")
        db.add_subscription(ua, "https://ex.com/feed.xml")
        db.add_subscription(ub, "https://ex.com/feed.xml")   # same feed, 2nd user
        db.add_subscription(ub, "https://broken.example/feed")

        def fetch(feed_url):
            if "broken" in feed_url:
                raise monitor.FeedError("dead feed")
            return [{"url": "https://ex.com/drop", "title": "Drop"}]

        analyzed = []

        async def fake_analyze_url(url, **kw):
            analyzed.append(kw["save_for_user_id"])
            return _fake_result()

        monkeypatch.setattr(monitor, "fetch_entries", fetch)
        monkeypatch.setattr(pipeline, "analyze_url", fake_analyze_url)

        asyncio.run(monitor.poll_all_subscriptions())  # seeds both good subs
        totals = asyncio.run(monitor.poll_all_subscriptions())
        # Seeded entries don't re-analyze; the dead feed doesn't stop the run.
        assert totals["feed_errors"] == 1
        assert totals["subscriptions"] == 3
        # New drop for both subscribers next cycle:
        def fetch2(feed_url):
            if "broken" in feed_url:
                raise monitor.FeedError("dead feed")
            return [{"url": "https://ex.com/drop2", "title": "Drop 2"}]
        monkeypatch.setattr(monitor, "fetch_entries", fetch2)
        totals = asyncio.run(monitor.poll_all_subscriptions())
        assert totals["analyzed"] == 2
        assert sorted(analyzed) == sorted([ua, ub])  # one personalized run each


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class TestSubscriptionEndpoints:
    @pytest.fixture
    def resolved(self, monkeypatch):
        import bot.monitor
        monkeypatch.setattr(
            bot.monitor, "resolve_feed",
            lambda url: {"feed_url": "https://ex.com/feed.xml", "title": "My Blog",
                         "source_kind": "rss"},
        )

    def test_create_list_delete_roundtrip(self, client, auth_headers, db, resolved):
        uid = db.upsert_user_by_email("u@example.com")
        r = client.post("/api/subscriptions", headers=auth_headers,
                        json={"user_id": uid, "url": "https://ex.com/blog"})
        assert r.status_code == 201
        sub = r.json()
        assert sub["feed_url"] == "https://ex.com/feed.xml"

        r = client.get(f"/api/subscriptions?user_id={uid}", headers=auth_headers)
        assert [s["title"] for s in r.json()] == ["My Blog"]

        r = client.delete(f"/api/subscriptions/{sub['id']}?user_id={uid}",
                          headers=auth_headers)
        assert r.status_code == 204
        assert client.get(f"/api/subscriptions?user_id={uid}", headers=auth_headers).json() == []

    def test_duplicate_subscription_is_409(self, client, auth_headers, db, resolved):
        uid = db.upsert_user_by_email("u@example.com")
        body = {"user_id": uid, "url": "https://ex.com/blog"}
        assert client.post("/api/subscriptions", headers=auth_headers, json=body).status_code == 201
        r = client.post("/api/subscriptions", headers=auth_headers, json=body)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "already-subscribed"

    def test_cap_is_409_with_message(self, client, auth_headers, db, resolved, monkeypatch):
        monkeypatch.setattr(db, "MAX_SUBSCRIPTIONS_PER_USER", 0)
        uid = db.upsert_user_by_email("u@example.com")
        r = client.post("/api/subscriptions", headers=auth_headers,
                        json={"user_id": uid, "url": "https://ex.com/blog"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "limit-reached"

    def test_unresolvable_feed_is_422(self, client, auth_headers, db, monkeypatch):
        import bot.monitor

        def boom(url):
            raise bot.monitor.FeedError("No feed found at that URL.")

        monkeypatch.setattr(bot.monitor, "resolve_feed", boom)
        uid = db.upsert_user_by_email("u@example.com")
        r = client.post("/api/subscriptions", headers=auth_headers,
                        json={"user_id": uid, "url": "https://ex.com/none"})
        assert r.status_code == 422
        assert "No feed found" in r.json()["detail"]["message"]

    def test_delete_is_ownership_scoped(self, client, auth_headers, db, resolved):
        owner = db.upsert_user_by_email("owner@example.com")
        other = db.upsert_user_by_email("other@example.com")
        r = client.post("/api/subscriptions", headers=auth_headers,
                        json={"user_id": owner, "url": "https://ex.com/blog"})
        sub_id = r.json()["id"]
        r = client.delete(f"/api/subscriptions/{sub_id}?user_id={other}", headers=auth_headers)
        assert r.status_code == 404
        assert len(client.get(f"/api/subscriptions?user_id={owner}", headers=auth_headers).json()) == 1

    def test_export_and_erasure_cover_subscriptions(self, client, auth_headers, db, resolved):
        uid = db.upsert_user_by_email("u@example.com")
        client.post("/api/subscriptions", headers=auth_headers,
                    json={"user_id": uid, "url": "https://ex.com/blog"})
        exported = client.get(f"/api/users/{uid}/export", headers=auth_headers).json()
        assert exported["subscriptions"][0]["feed_url"] == "https://ex.com/feed.xml"

        db.delete_user(uid)
        with db._get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM subscription_items").fetchone()[0] == 0
