import os

# Model and context limits
# claude-haiku-4-5: 200k token context (~800k chars) — 100k chars leaves ample room for prompt + response
# gpt-4o-mini: 128k token context (~500k chars)
MAX_CONTENT_CHARS = 100_000

# VIP accounts (the operator's own) get the deep-reading limits below instead of
# the standard ones: hours-long videos/podcasts transcribed in full and briefed
# part by part. Comma-separated `users.id` values; empty = nobody is VIP.
VIP_USER_IDS: frozenset[int] = frozenset(
    int(p) for p in os.getenv("VIP_USER_IDS", "").replace(" ", "").split(",") if p.isdigit()
)

# Ceiling on what a VIP source may feed the summarizer: ~800k chars ≈ 135k
# words ≈ 14 h of speech, matching VIP_WHISPER_MAX_DURATION_S in the fetcher.
VIP_MAX_CONTENT_CHARS = 800_000

# What the fetch layer returns (and url_cache stores) regardless of caller. The
# per-tier limit is applied afterwards in the pipeline, so one cached fetch
# serves both a standard user (cut to MAX_CONTENT_CHARS) and a VIP (cut to
# VIP_MAX_CONTENT_CHARS) and the pipeline knows how much was dropped.
FETCH_MAX_CHARS = 2_000_000


def is_vip(user_id: int | None) -> bool:
    return user_id is not None and user_id in VIP_USER_IDS


def max_content_chars_for(user_id: int | None) -> int:
    """Per-caller cap on source text fed to the summarizer."""
    return VIP_MAX_CONTENT_CHARS if is_vip(user_id) else MAX_CONTENT_CHARS

# Contact address sent to open scholarly APIs (Crossref polite pool, Unpaywall
# requires it). Not secret — it's an identifier so the APIs can reach us if our
# traffic misbehaves. Override per-deploy via CONTACT_EMAIL.
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "hello@filter.fyi")

# Proxy for all YouTube traffic (#128): YouTube blocks Fly's datacenter IPs, so
# on Fly this is tailscaled's local SOCKS5 server, which exits via the home
# MacBook (see docker-entrypoint.sh). Empty = go direct (local dev).
YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY", "")
