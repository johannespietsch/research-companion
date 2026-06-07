# Research Companion

A Telegram bot that acts as a personal AI research analyst. Send it links, articles, voice memos, photos, PDFs, or raw text and it returns structured analysis with actionable next steps. Everything is stored in a local SQLite knowledge base you can search and browse from the CLI.

## What It Does

1. **Ingest** -- send any content to the bot via Telegram (URLs, text, voice, video, photos, documents)
2. **Extract** -- fetches and extracts text from the source (smart handling for YouTube, Twitter/X, articles, PDFs, audio transcription)
3. **Analyze** -- an LLM produces a structured breakdown: main idea, why it matters, the source point it's grounded in, category, a quick win (+ first step) and a bigger play, time to explore, and a watch/skim/skip verdict
4. **Hand off** -- each suggestion ships a tool-agnostic "try this" brief the reader can paste straight into their own assistant (ChatGPT, Claude, Cursor, Codex…) to plan and execute — built by `bot/agent_brief.py`, zero extra inference cost
5. **Store** -- saves the original content, analysis, and your context message to a local knowledge base
6. **Browse** -- query and review your knowledge base from the CLI

## Supported Input Types

| Input | How It's Processed |
|---|---|
| URLs (articles) | HTML extracted via trafilatura |
| PDF URLs | Downloaded and text extracted with pdfplumber |
| YouTube links | Transcript fetched (fallback: yt-dlp description) |
| Vimeo links | Subtitles via yt-dlp (fallback: Whisper transcription) |
| StreamYard links | Headless browser intercepts signed MP4, transcribed with Whisper |
| Twitter/X links | fxtwitter API > X syndication API > yt-dlp (including X Articles/Notes) |
| Plain text | Analyzed directly |
| Voice messages | Transcribed with Whisper, then analyzed |
| Audio files | Transcribed with Whisper (MP3, OGG, M4A, WAV, FLAC) |
| Videos / video notes | Audio extracted, transcribed, then analyzed |
| Photos | Vision model extracts text and key info, then analyzed |
| PDFs | Text extracted with pdfplumber |
| Text documents | Read directly |

## Setup

### Prerequisites

- Python 3.11+
- ffmpeg (required by faster-whisper for audio/video transcription)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- At least one AI API key (Anthropic or OpenAI)

### Installation

```bash
git clone <repo-url>
cd research-companion
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Optionally set up a personal profile (see [Personalisation](#personalisation)):

```bash
cp PROFILE.md.example PROFILE.md
# then edit PROFILE.md
```

Create a `.env` file:

```bash
# Required
TELEGRAM_TOKEN=your-telegram-bot-token

# AI provider (at least one required; Anthropic preferred if both set)
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key

# Optional -- set for production webhook mode
WEBHOOK_URL=https://your-domain.com

# Required for the public /api/try endpoint -- the filter.fyi Cloudflare
# Worker authenticates with a matching `x-filter-fyi-secret` header.
FILTER_FYI_TRY_SECRET=long-random-string

# Optional -- override the data directory (SQLite DB + file store).
# Set to /data in containerised deploys with a mounted volume.
# DATA_DIR=/data

# Optional -- enable the daily error-log scanner that files GH issues for
# unhandled bugs (see "Error log + auto-filed bug issues" below). Disabled by
# default so local dev doesn't post issues by accident.
# SCAN_ERRORS_ENABLED=true
# SCAN_ERRORS_HOUR_UTC=3
# GH_REPO=johannespietsch/research-companion
# GitHub App credentials (issues are filed as <app-name>[bot]):
# GH_APP_ID=123456
# GH_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
# GH_APP_INSTALLATION_ID=  # optional; auto-discovered from GH_REPO if unset
```

## Usage

### Running the Bot

**Local development** (polling, no public URL needed):

```bash
python main.py
```

**Production** (webhook via FastAPI/uvicorn):

```bash
export WEBHOOK_URL=https://your-domain.com
uvicorn main:app --host 0.0.0.0 --port 8080
```

The mode is selected automatically -- if `WEBHOOK_URL` is set, it builds a FastAPI app with a `/webhook` endpoint and `/health` check. Otherwise it runs in long-polling mode.

### Bot Commands

Once the bot is running, use these commands inside the Telegram chat:

| Command | Description |
|---|---|
| `/list` | Browse the 20 most recent knowledge base entries |
| `/show <id>` | Show full analysis and metadata for an entry |
| `/search <query>` | Search across source, content, and analysis |
| `/delete <id>` | Remove an entry from the knowledge base |
| `/profile` | Show your current profile |
| `/profile <text>` | Update your profile with a one-liner |
| `/token` | Generate (or regenerate) your web UI API token |

Each Telegram user has their own isolated knowledge base — all commands only operate on the authenticated user's data.

**Register commands with BotFather** (optional, enables autocomplete in Telegram):

1. Message [@BotFather](https://t.me/BotFather) → `/setcommands`
2. Select your bot
3. Paste:

```
list - Browse recent knowledge base entries
show - /show <id>  Show full entry
search - /search <query>  Search the knowledge base
delete - /delete <id>  Remove an entry
profile - Show or update your personal profile
token - Generate your web UI API token
```

### Knowledge Base CLI

```bash
python kb.py                    # List all saved items
python kb.py <id>               # Show full item (original content + analysis)
python kb.py search <query>     # Full-text search across all fields
python kb.py delete <id>        # Delete an item
```

Example list output:

```
  ID  TYPE          DATE              NOTE                  SOURCE
--------------------------------------------------------------------------------
  10  🔗 url        2026-03-06T13:03  check this out        https://example.com/article
   9  🎙 voice_memo 2026-03-06T12:50   - NA -
   8  📄 document   2026-03-06T12:45   - NA -               report.pdf
```

### Debugging the pipeline

Three scripts walk the URL → analysis chain step by step and surface the internal length limits that bound each stage. Use them to see exactly where a piece of content is being truncated (`MAX_CONTENT_CHARS` on the fetched text, requested `max_tokens` on the summary call, `SUMMARY_MAX_CHARS` on the stored brief) and which model the dispatch resolves to.

```bash
python -m scripts.debug_transcript <url>                          # fetched text + char limits
python -m scripts.debug_summary <url> [--anon] [--no-cache]       # + summary, with token-cap fill ratio
python -m scripts.debug_verdict <url> [--anon] [--profile PROFILE.md]  # + verdict; profile defaults to DEFAULT_PROFILE
```

`debug_summary` and `debug_verdict` default to the **signed-in path** (`ctx.user_id=1` → Sonnet 4.6 on summary + analyze), matching what a real signed-in user sees. Pass `--anon` to simulate the anonymous `/api/try` caller (Haiku throughout).

`--no-cache` on `debug_summary` bypasses the content-addressed cache so you see a fresh LLM call after changing prompts or token caps (the cache key doesn't include `max_tokens`, so a stale truncated summary would otherwise be returned).

### REST API (Web UI)

The bot exposes a token-authenticated REST API for use by a web UI or external tools. Authenticate using the token generated by `/token`:

```
Authorization: Bearer <your-token>
```

Key endpoints (all under `/api`):

| Endpoint | Description |
|---|---|
| `GET /api/items` | List all items (optional `?q=` for search) |
| `GET /api/items/<id>` | Get a single item |
| `DELETE /api/items/<id>` | Delete an item |
| `POST /api/ingest/url` | Ingest a URL |
| `POST /api/ingest/file` | Upload and ingest a file |

### Public API (anonymous)

Used by the [filter.fyi](https://filter.fyi) landing page so anonymous visitors can submit a URL and get a verdict without signing up. All endpoints authenticated via a shared secret (`x-filter-fyi-secret`) rather than a user token.

#### Async job API (current)

The Worker uses a two-step async flow to avoid Cloudflare's 25s upstream timeout:

**Step 1 — start a job**

```
POST /api/job
content-type: application/json
x-filter-fyi-secret: <FILTER_FYI_TRY_SECRET>

{ "url": "https://example.com/post" }
```

Response (202):

```json
{ "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" }
```

The backend creates a DB row and immediately returns; the actual fetch → summarise → analyse pipeline runs as a background task.

**Step 2 — poll for result**

```
GET /api/job/<job_id>
x-filter-fyi-secret: <FILTER_FYI_TRY_SECRET>
```

| Response | Meaning |
|----------|---------|
| `{"status":"pending"}` | Still running |
| `{"status":"done","result":"<JSON string>"}` | Complete — `result` is the same shape as the `/api/try` response below |
| `{"status":"error","error":"<code>"}` | Failed — same error codes as `/api/try` |
| 404 | Unknown or expired job (TTL: 1 hour) |

#### Legacy sync endpoint

`POST /api/try` is still active for backward compatibility but is no longer used by the Worker. It executes fetch + summarise + analyse synchronously, which can exceed the Worker timeout for slow pages.

```
POST /api/try
content-type: application/json
x-filter-fyi-secret: <FILTER_FYI_TRY_SECRET>

{ "url": "https://example.com/post" }
```

Response shape:

```jsonc
{
  "url": "https://example.com/post",
  "title": "…",
  "source_type": "article" | "youtube" | "social" | "pdf",
  "image_urls": [],
  "content_preview": "first ~2000 chars of extracted text",
  "verdict": "watch" | "skim" | "skip",
  "analysis": {
    "main_idea": "…",
    "why_it_matters": "…",
    "grounded_in": "the specific source claim the actions rest on",
    "category": "kebab-case",
    "quick_win": "imperative 30–90 min action",
    "first_step": "one copy-pasteable opening move for the quick win",
    "bigger_play": "the more ambitious multi-week arc",
    "time_required": "12 min read"
  },
  // One "try this" handoff per tier, built from the fields above (pure
  // templating — no extra LLM call). `brief` is the paste-anywhere text.
  "actions": [
    { "kind": "quick_win",   "label": "⚡ Quick win",   "text": "…", "brief": "…" },
    { "kind": "bigger_play", "label": "🚀 Bigger play", "text": "…", "brief": "…" }
  ]
}
```

Error responses (Worker maps these to user-friendly notices):

| Status | Body                            | Cause                                       |
|--------|---------------------------------|---------------------------------------------|
| 400    | `{"error":"invalid-url"}`       | Not a valid http(s) URL                     |
| 401    | `{"error":"unauthorized"}`      | Missing or wrong `x-filter-fyi-secret`      |
| 422    | `{"error":"no-transcript"}`     | YouTube/video with no transcript available  |
| 422    | `{"error":"extraction-failed"}` | Couldn't pull text from the page            |
| 502    | `{"error":"fetch-failed"}`      | Upstream fetch crashed                      |
| 502    | `{"error":"analyze-failed"}`    | LLM call crashed                            |

Both endpoints are **stateless**: nothing is persisted to the bot's SQLite. The Worker is the system of record for anonymous tries (D1 `anon_summaries` table, keyed by `anon_id` for later claim-on-signup).

## Deployment (Fly.io)

The backend runs as a single long-lived container with a mounted volume for the SQLite DB and uploaded files. First-time setup:

```bash
fly launch --no-deploy --name filter-fyi-backend --region fra
fly volume create filter_fyi_data --region fra --size 3
fly secrets set \
  ANTHROPIC_API_KEY=... \
  TELEGRAM_TOKEN=... \
  FILTER_FYI_TRY_SECRET=... \
  WEBHOOK_URL=https://filter-fyi-backend.fly.dev
fly deploy
```

After that, `fly deploy` from this directory ships changes. The volume mounts at `/data` (`DATA_DIR=/data` in `fly.toml`) so the DB and `data/files/` persist across redeploys.

**Wiring to the frontend Worker:** set the Worker's `BOT_API_URL` to `https://<your-fly-app>.fly.dev/api/try` and `BOT_API_KEY` to the same string as `FILTER_FYI_TRY_SECRET`:

```bash
cd ../filter.fyi-frontend
npx wrangler secret put BOT_API_URL
npx wrangler secret put BOT_API_KEY
```

## Error log + auto-filed bug issues

WARNING+ log records are captured to an `error_log` table in the SQLite DB. A daily background task inside the bot reads the last 24h of records, groups by fingerprint, asks Claude Haiku to classify each group as one of:

- **known_user_limit** — already covered by `bot/fetch_errors.py` (PDF download failed, video too long for Whisper, rate-limited, …); the user already sees a friendly message. Ignored.
- **bug** — unexpected exception; a GH issue is filed against this repo. Dedup is by `<!-- fingerprint: ... -->` marker in the body.
- **noise** — not actionable. Ignored.

It runs **in-process** (not as a separate Fly machine) because the SQLite DB lives on a single-attach volume, so it can't be read from a second machine simultaneously.

### Authentication: GitHub App

Issues are filed by a GitHub App so they appear as `<app-name>[bot]` rather than a personal account, and so the bot authenticates with short-lived (≈1h) installation tokens minted from a private key instead of a long-lived PAT.

One-time setup:

1. Create the App at **Settings → Developer settings → GitHub Apps → New GitHub App** (or [the docs](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/about-creating-github-apps)). Repository permission **Issues: Read & write** is all it needs; no webhook.
2. Generate a private key (downloads a `.pem`) and note the **App ID**.
3. **Install** the App on this repository (App settings → Install App).
4. Set the secrets on Fly (the private key is multi-line — `fly secrets set` accepts it directly, or pass it with literal `\n`):

```bash
fly secrets set \
  SCAN_ERRORS_ENABLED=true \
  GH_APP_ID=123456 \
  GH_APP_PRIVATE_KEY="$(cat path/to/app.private-key.pem)"
```

The installation ID is auto-discovered from `GH_REPO`; set `GH_APP_INSTALLATION_ID` only to skip that lookup.

Run manually for testing (won't post issues with `--dry-run`):

```bash
make scan-errors-dry
make scan-errors            # actually files issues; needs the GH App secrets
```

## Personalisation

All analysis is tailored to you via `PROFILE.md` in the project root (gitignored — stays private). The bot reads it on every request, so edits take effect immediately without restarting. A starter template is provided as `PROFILE.md.example`.

**Quick update from Telegram:**
```
/profile I'm a software engineer focused on AI and crypto. Suggest Python experiments under a day.
```

**Multi-line profile** — edit `PROFILE.md` directly in any text editor. Markdown comments (`<!-- … -->`) are stripped by the bot so you can annotate freely.

**Check current profile:**
```
/profile
```

If `PROFILE.md` is absent or empty the bot falls back to a generic prompt, so personalisation is entirely optional.

## Project Structure

```
research-companion/
├── main.py              # Entry point: polling (dev) or webhook (prod)
├── kb.py                # CLI knowledge base browser
├── requirements.txt     # Python dependencies
├── .env                 # Environment variables (not committed)
├── PROFILE.md.example   # Profile template — copy to PROFILE.md and edit freely
├── research.db          # SQLite database (created at runtime, not committed)
├── data/files/          # Persistent file store for uploaded/downloaded media
├── scripts/
│   ├── debug_transcript.py  # Step 1 of the pipeline — fetch raw text for a URL
│   ├── debug_summary.py     # Step 2 — fetch + summarise, with limits surfaced
│   ├── debug_verdict.py     # Step 3 — full pipeline ending in a verdict
│   ├── prune.py             # Daily DB pruning (error_log, url_cache, expired codes)
│   └── scan_errors.py       # Daily error-log → GitHub-issue scanner
└── bot/
    ├── application.py   # Telegram app builder, registers handlers
    ├── handlers.py      # Message handlers for each input type
    ├── commands.py      # Telegram command handlers (/list, /show, /search, etc.)
    ├── analyzer.py      # LLM analysis (Anthropic / OpenAI)
    ├── fetcher.py       # URL content extraction (YouTube, Vimeo, X, PDF, generic)
    ├── transcriber.py   # Audio/video transcription (Whisper)
    ├── formatting.py    # Analysis formatting helpers
    ├── db.py            # SQLite interface + schema migrations (per-user)
    ├── auth.py          # Bearer token authentication for the REST API
    ├── api.py           # REST API endpoints (FastAPI router, token-authenticated)
    ├── storage.py       # Persistent file store helpers
    └── config.py        # Shared constants (MAX_CONTENT_CHARS, etc.)
```

## Database Schema

```sql
CREATE TABLE items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT,     -- url, note, voice_memo, audio, video, photo, document
    source      TEXT,     -- URL, filename, or identifier
    content     TEXT,     -- original extracted text / transcription
    analysis    TEXT,     -- LLM analysis output
    user_note   TEXT,     -- context message from the user (caption, surrounding text)
    created_at  TEXT      -- ISO 8601 timestamp
);
```

Existing databases are migrated automatically on startup.

## AI Providers

The Anthropic path dispatches per (task, user tier) so signed-in users get the more capable Sonnet on the text-heavy steps while anon traffic stays on Haiku for cost. See `bot.analyzer._resolve_model`.

| Task | Anon | Signed-in |
|---|---|---|
| summary | claude-haiku-4-5 | claude-sonnet-4-6 |
| analyze (verdict) | claude-haiku-4-5 | claude-sonnet-4-6 |
| image (chart/screenshot extraction) | claude-haiku-4-5 | claude-haiku-4-5 |

If only `OPENAI_API_KEY` is set the bot falls back to `gpt-4o-mini` for every task — per-tier routing is Anthropic-only.

Both text analysis and vision (photo) analysis are supported through either provider.

## Dependencies

| Package | Purpose |
|---|---|
| python-telegram-bot | Telegram Bot API |
| fastapi + uvicorn | Webhook server (production) |
| anthropic / openai | LLM analysis |
| trafilatura | Article text extraction from HTML |
| youtube-transcript-api | YouTube transcript fetching |
| yt-dlp | Video metadata extraction (YouTube, X fallback) |
| faster-whisper | Speech-to-text (Whisper base model, CPU, int8) |
| pdfplumber | PDF text extraction |
| httpx | Async HTTP client |

## License

MIT
