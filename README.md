# Research Companion

A Telegram bot that acts as a personal AI research analyst. Send it links, articles, voice memos, photos, PDFs, or raw text and it returns structured analysis with actionable next steps. Everything is stored in a local SQLite knowledge base you can search and browse from the CLI.

## What It Does

1. **Ingest** -- send any content to the bot via Telegram (URLs, text, voice, video, photos, documents)
2. **Extract** -- fetches and extracts text from the source (smart handling for YouTube, Twitter/X, articles, PDFs, audio transcription)
3. **Analyze** -- an LLM produces a structured breakdown: main idea, why it matters, category, suggested experiment, time to explore
4. **Store** -- saves the original content, analysis, and your context message to a local knowledge base
5. **Browse** -- query and review your knowledge base from the CLI

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
| Inbound email | Parsed via Mailgun webhook, body and attachments analyzed |

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

# Optional -- Mailgun inbound email webhook security (HMAC-SHA256 verification)
MAILGUN_SIGNING_KEY=your-mailgun-signing-key

# Required for the public /api/try endpoint -- the filter.fyi Cloudflare
# Worker authenticates with a matching `x-filter-fyi-secret` header.
FILTER_FYI_TRY_SECRET=long-random-string

# Optional -- override the data directory (SQLite DB + file store).
# Set to /data in containerised deploys with a mounted volume.
# DATA_DIR=/data
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

### Public `/api/try` (anonymous)

Used by the [filter.fyi](https://filter.fyi) landing page so anonymous visitors can submit a URL and get a verdict without signing up. Authenticated via a shared secret rather than a user token:

```
POST /api/try
content-type: application/json
x-filter-fyi-secret: <FILTER_FYI_TRY_SECRET>

{ "url": "https://example.com/post" }
```

Response shape (the contract the Cloudflare Worker consumes):

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
    "category": "kebab-case",
    "suggested_experiment": "…",
    "time_required": "12 min read"
  }
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

The endpoint is **stateless**: nothing is persisted to the bot's SQLite. The Worker is the system of record for anonymous tries (D1 `summaries` table, keyed by `anon_id` for later claim-on-signup).

### Inbound Email

Forward emails to the bot via Mailgun's inbound parse webhook. The sender's email address is matched against registered user profiles.

1. Set up a Mailgun receiving route pointing to `https://your-domain.com/inbound-email`
2. Register your email in your profile via the web UI
3. Optionally set `MAILGUN_SIGNING_KEY` in `.env` to verify webhook signatures

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
    ├── email_handler.py # Mailgun inbound email webhook handler
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

| Provider | Model | Used When |
|---|---|---|
| Anthropic | claude-haiku-4-5 | `ANTHROPIC_API_KEY` is set (preferred) |
| OpenAI | gpt-4o-mini | Fallback when only `OPENAI_API_KEY` is set |

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
