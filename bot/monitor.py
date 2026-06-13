"""Channel monitoring (#68): poll the feeds a user follows, run new drops
through the normal analysis pipeline.

Subscribe time does the hard part: any supported input (a raw RSS/Atom URL, a
YouTube channel/handle URL, or an HTML page that advertises a feed via
``<link rel="alternate">``) is resolved to one canonical feed URL, so the
poller only ever speaks RSS/Atom.

Poll cycles run in-process (single-machine SQLite, like the digest/prune
loops). Every new entry flows through ``bot.pipeline.analyze_url`` with the
subscriber's user_id — lens personalization, llm_calls cost attribution, the
url_cache fetch dedupe across subscribers, and the capacity guards all apply
for free. Monitoring is background load: a busy box (ERR_BUSY) leaves entries
pending for the next cycle rather than competing with interactive requests.

Cost bounds, in order of importance:
- subscribing NEVER backfills — entries present at subscribe time are recorded
  as 'seeded' and not analyzed;
- at most MAX_NEW_PER_POLL entries per subscription per cycle, and at most
  MAX_ANALYSES_PER_CYCLE across all subscriptions;
- per-user subscription cap lives in the DB layer (MAX_SUBSCRIPTIONS_PER_USER).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from bot.ssrf import BlockedURLError, assert_public_url

logger = logging.getLogger(__name__)

# How many of the newest feed entries we look at per poll. Feeds usually carry
# 15–50; anything older than this window has scrolled past us and is ignored.
FEED_ENTRY_WINDOW = 25
# New entries analyzed per subscription per cycle.
MAX_NEW_PER_POLL = 3
# Hard ceiling on LLM-bearing work per poll cycle across all subscriptions.
MAX_ANALYSES_PER_CYCLE = 12

_FETCH_TIMEOUT_S = 20
_MAX_BODY_BYTES = 5 * 1024 * 1024
_UA = "filter.fyi feed monitor (+https://filter.fyi)"

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
# A YouTube channel page exposes its UC… id in several spots. We try the most
# stable ones (canonical link / og:url always point at the /channel/UC… form
# even from a handle page), so a markup tweak to any single field can't break
# resolution.
_YT_CHANNEL_ID_RES = [
    re.compile(r'"channelId"\s*:\s*"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'"externalId"\s*:\s*"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'<link[^>]+rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'<meta[^>]+property="og:url"[^>]+content="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"'),
]
# YouTube serves an EU "consent" interstitial to datacenter IPs (e.g. Fly),
# which carries none of the channel markup. This cookie records the consent
# and is what every YouTube-feed tool sends to get the real page.
_YT_CONSENT_COOKIE = "SOCS=CAI"
_FEED_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]*>', re.IGNORECASE
)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_FEED_TYPE_RE = re.compile(r'type=["\']application/(rss|atom)\+xml["\']', re.IGNORECASE)


class FeedError(Exception):
    """A user-facing problem with a feed URL (unreachable, no feed found…)."""


def _is_youtube(url: str) -> bool:
    try:
        return urlparse(url).hostname in _YT_HOSTS
    except ValueError:
        return False


def _get(url: str) -> httpx.Response:
    """SSRF-guarded GET with a size cap. Re-checks the final URL after
    redirects (per bot/ssrf.py's documented limitation). Sends the YouTube
    consent cookie so datacenter IPs get the real channel page, not the EU
    consent wall."""
    assert_public_url(url)
    headers = {"User-Agent": _UA}
    if _is_youtube(url):
        headers["Cookie"] = _YT_CONSENT_COOKIE
    resp = httpx.get(
        url, timeout=_FETCH_TIMEOUT_S, follow_redirects=True, headers=headers,
    )
    assert_public_url(str(resp.url))
    resp.raise_for_status()
    if len(resp.content) > _MAX_BODY_BYTES:
        raise FeedError("Feed is too large to process.")
    return resp


def _looks_like_feed(body: str) -> bool:
    head = body.lstrip()[:500].lower()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head


def _discover_feed_in_html(body: str, base_url: str) -> str | None:
    """First advertised RSS/Atom <link rel="alternate"> in an HTML page."""
    for tag in _FEED_LINK_RE.findall(body):
        if not _FEED_TYPE_RE.search(tag):
            continue
        href = _HREF_RE.search(tag)
        if href:
            return urljoin(base_url, href.group(1))
    return None


def _youtube_feed_url(url: str, body: str | None) -> str | None:
    """Map any YouTube channel-ish URL to its Atom feed."""
    parsed = urlparse(url)
    if parsed.hostname not in _YT_HOSTS:
        return None
    m = re.match(r"^/channel/(UC[0-9A-Za-z_-]{22})", parsed.path)
    if m:
        return _YT_FEED.format(cid=m.group(1))
    # Handle/legacy forms (/@name, /c/name, /user/name) carry the channelId
    # only inside the page markup.
    if body:
        for pat in _YT_CHANNEL_ID_RES:
            m = pat.search(body)
            if m:
                return _YT_FEED.format(cid=m.group(1))
    return None


def resolve_feed(url: str) -> dict:
    """Resolve any supported input URL to a canonical feed.

    Returns {feed_url, title, source_kind}. Raises FeedError (user-facing) or
    BlockedURLError (non-public target).
    """
    url = (url or "").strip()
    if not url:
        raise FeedError("That doesn't look like a URL.")
    if "://" not in url:
        url = "https://" + url

    # Pure-path YouTube channel form needs no page fetch at all.
    direct_yt = _youtube_feed_url(url, None)
    if direct_yt:
        feed_url, kind = direct_yt, "youtube"
    else:
        try:
            resp = _get(url)
        except BlockedURLError:
            raise
        except FeedError:
            raise
        except Exception as e:
            raise FeedError(f"Couldn't reach that URL ({e.__class__.__name__}).")
        body = resp.text
        if _looks_like_feed(body):
            feed_url, kind = str(resp.url), "rss"
        else:
            yt = _youtube_feed_url(str(resp.url), body)
            if yt:
                feed_url, kind = yt, "youtube"
            else:
                discovered = _discover_feed_in_html(body, str(resp.url))
                if not discovered:
                    raise FeedError(
                        "No feed found at that URL. Paste an RSS/Atom feed URL, "
                        "a YouTube channel URL, or a site that advertises a feed."
                    )
                feed_url, kind = discovered, "rss"

    # Validate the resolved feed and pick up its title.
    try:
        feed_resp = _get(feed_url)
    except BlockedURLError:
        raise
    except Exception as e:
        raise FeedError(f"Couldn't fetch the feed ({e.__class__.__name__}).")
    parsed = feedparser.parse(feed_resp.content)
    if parsed.bozo and not parsed.entries:
        raise FeedError("That URL doesn't parse as an RSS/Atom feed.")
    title = (getattr(parsed.feed, "title", "") or "").strip()
    return {"feed_url": feed_url, "title": title, "source_kind": kind}


def fetch_entries(feed_url: str) -> list[dict]:
    """The newest FEED_ENTRY_WINDOW entries of a feed: {url, title}."""
    resp = _get(feed_url)
    parsed = feedparser.parse(resp.content)
    out = []
    for e in parsed.entries[:FEED_ENTRY_WINDOW]:
        link = (getattr(e, "link", "") or "").strip()
        if not link:
            continue
        out.append({"url": link, "title": (getattr(e, "title", "") or "").strip()})
    return out


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

async def poll_subscription(sub, *, analysis_budget: int) -> dict:
    """One subscription: register new entries, analyze up to the budget.

    Returns {"new": n, "analyzed": n, "errors": n, "busy": bool}.
    """
    import asyncio

    from bot.analyzer import UsageContext
    from bot.db import (
        get_pending_subscription_entries,
        mark_subscription_polled,
        record_subscription_entry,
        set_subscription_entry_result,
    )
    from bot.pipeline import ERR_BUSY, PipelineError, analyze_url

    stats = {"new": 0, "analyzed": 0, "errors": 0, "busy": False}
    entries = await asyncio.to_thread(fetch_entries, sub["feed_url"])

    first_poll = not sub["last_polled_at"]
    # Subscribing must not trigger a backfill: everything already in the feed
    # is recorded as seen-but-never-analyzed.
    status = "seeded" if first_poll else "pending"
    for e in entries:
        if record_subscription_entry(sub["id"], sub["user_id"], e["url"], e["title"],
                                     status=status):
            stats["new"] += 1
    mark_subscription_polled(sub["id"])
    if first_poll:
        return stats

    for entry in get_pending_subscription_entries(sub["id"], limit=MAX_NEW_PER_POLL):
        if stats["analyzed"] >= analysis_budget:
            break
        try:
            result = await analyze_url(
                entry["entry_url"],
                ctx=UsageContext(user_id=sub["user_id"]),
                save_for_user_id=sub["user_id"],
            )
            set_subscription_entry_result(
                entry["id"], status="done", item_id=result.saved_id,
                verdict=(result.analysis.get("verdict") or ""),
            )
            stats["analyzed"] += 1
        except PipelineError as e:
            if e.code == ERR_BUSY:
                # Leave pending — the box is saturated with interactive work;
                # monitoring is the load that sheds first.
                stats["busy"] = True
                break
            set_subscription_entry_result(entry["id"], status="error")
            stats["errors"] += 1
    return stats


async def poll_all_subscriptions() -> dict:
    """One cycle over every subscription. Per-subscription failures are
    contained; the cycle-wide analysis ceiling bounds LLM spend."""
    from bot.db import get_all_subscriptions

    totals = {"subscriptions": 0, "new": 0, "analyzed": 0, "errors": 0, "feed_errors": 0}
    budget = MAX_ANALYSES_PER_CYCLE
    for sub in get_all_subscriptions():
        totals["subscriptions"] += 1
        try:
            stats = await poll_subscription(sub, analysis_budget=budget)
        except Exception:
            totals["feed_errors"] += 1
            logger.warning("poll failed for subscription %s (%s)",
                           sub["id"], sub["feed_url"], exc_info=True)
            continue
        budget -= stats["analyzed"]
        totals["new"] += stats["new"]
        totals["analyzed"] += stats["analyzed"]
        totals["errors"] += stats["errors"]
        if stats["busy"] or budget <= 0:
            break
    return totals
