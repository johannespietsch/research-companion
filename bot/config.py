import os

# Model and context limits
# claude-haiku-4-5: 200k token context (~800k chars) — 100k chars leaves ample room for prompt + response
# gpt-4o-mini: 128k token context (~500k chars)
MAX_CONTENT_CHARS = 100_000

# Contact address sent to open scholarly APIs (Crossref polite pool, Unpaywall
# requires it). Not secret — it's an identifier so the APIs can reach us if our
# traffic misbehaves. Override per-deploy via CONTACT_EMAIL.
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "hello@filter.fyi")
