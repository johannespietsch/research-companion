# Operations

Runtime, durability, and maintenance notes for the filter.fyi backend (Fly).

## Data durability — backups

The canonical data (users, items, link codes) is a single SQLite file at
`${DATA_DIR}/research.db` on a **single Fly volume**. A volume is attached to
one machine at a time and is **not** itself a backup. Two layers protect it:

### 1. Fly volume snapshots (baseline)
Fly takes automatic daily snapshots of the volume (default 5-day retention).
Verify / extend retention:

```sh
fly volumes list
fly volumes snapshots list <volume-id>
fly volumes update <volume-id> --snapshot-retention 30
```

Snapshots are coarse (up to ~24h of data loss, not point-in-time).

### 2. Litestream continuous replication (recommended, point-in-time)
[Litestream](https://litestream.io) streams the SQLite WAL to object storage,
giving near-real-time, point-in-time recovery. It's baked into the image but
**only runs when `LITESTREAM_REPLICA_URL` is set** — otherwise the container
starts plain uvicorn (see `docker-entrypoint.sh`), so local dev is unaffected.

To enable, provision an S3-compatible bucket (Cloudflare R2, Fly's Tigris,
Backblaze B2, …) and set Fly secrets. On **Litestream 0.3.x the endpoint and
region are config fields, not URL query params** — set them as their own env
vars (see [`litestream.yml`](../litestream.yml)), or a non-AWS target silently
resolves to AWS S3 and fails:

```sh
fly secrets set -a filter-fyi-backend \
  LITESTREAM_REPLICA_URL="s3://<bucket>/filter-fyi-backend" \
  LITESTREAM_ENDPOINT="https://<accountid>.r2.cloudflarestorage.com" \
  LITESTREAM_REGION="auto" \
  LITESTREAM_ACCESS_KEY_ID="<key>" \
  LITESTREAM_SECRET_ACCESS_KEY="<secret>"
# Tigris: endpoint https://fly.storage.tigris.dev, region auto.
# Plain AWS S3: omit LITESTREAM_ENDPOINT; set the real region.
```

The R2 access key + secret come from an **R2 API token** (R2 → Manage R2 API
Tokens → Object Read & Write); the secret is shown only once. The endpoint's
`<accountid>` is the host prefix of the bucket's S3 API URL.

Config: [`litestream.yml`](../litestream.yml) (30-day retention, daily
snapshots). On boot the entrypoint runs `litestream restore -if-db-not-exists`,
so a fresh/recovered volume self-heals from the replica.

> Tip: if a restore fails with a host/DNS error against R2, add
> `force-path-style: true` to the replica in `litestream.yml`.

**Restore manually:**
```sh
litestream restore -config litestream.yml -o /data/research.db "$LITESTREAM_REPLICA_URL"
```

> Test a restore into a throwaway path before relying on it.

## Disk growth & retention

The Fly volume is fixed-size; unbounded tables will eventually fill it and take
the app down. `bot.db.prune_maintenance()` deletes rows nothing needs:
- `error_log` older than 30 days (the scanner only looks back 24h)
- `url_cache` past its TTL
- expired, unredeemed `link_codes`

Run it daily — either a scheduled Fly machine or cron:
```sh
python -m scripts.prune
```

The other growth vector is **user-uploaded files** (PDFs, images, audio a user
submits directly), saved under the file store and referenced by
`items.file_path`. Note this is *not* scraped third-party media: URL / YouTube /
StreamYard content is downloaded to a temp dir, transcribed, and deleted — only
the derived brief is persisted (`items.content`), never the source media or its
full text. Uploaded files for deleted users are removed by account erasure, but
monitor disk usage:
```sh
fly volumes list           # check % used
```

## Capacity & scaling

The backend is a **single Fly machine** (`min_machines_running = 1`) because the
canonical SQLite DB lives on one volume that attaches to one machine at a time —
so **you cannot `fly scale count > 1`** without re-architecting storage. Vertical
scaling is the lever.

Three guards keep a traffic spike (e.g. a Product Hunt launch) from tipping the
box into the OOM / 25s-timeout failure mode in the runbook below:

1. **Heavy-work semaphore** (`bot/concurrency.py`) — caps concurrent fetch+LLM
   requests (`/try`, `/submit/url`, `/submit/file`). Over the cap, callers are
   shed with a fast `503 {"error":"busy"}` instead of queuing past the Worker's
   timeout. Tune with `HEAVY_CONCURRENCY` (default 8) / `HEAVY_ACQUIRE_TIMEOUT_S`.
2. **Thread pool** sized by `EXECUTOR_WORKERS` (default 16) — the IO-bound LLM
   calls run here; explicit so it doesn't depend on how Fly reports CPUs.
3. **Fly edge concurrency** (`fly.toml`): `hard_limit = 20` stops a burst from
   parking dozens of connections on the box.

**Before a launch / expected spike**, scale the VM up — it's reversible:

```sh
fly scale show -a filter-fyi-backend
fly scale vm shared-cpu-4x --memory 4096 -a filter-fyi-backend   # 4 CPU / 4 GB
# then raise the app guards to match the bigger box:
fly secrets set -a filter-fyi-backend HEAVY_CONCURRENCY=16 EXECUTOR_WORKERS=32
# and bump hard_limit in fly.toml before `fly deploy`.
```

Watch `fly logs` for `heavy_slot: shedding request` (you're at the cap → scale
up) and machine CPU/RAM in the Fly dashboard. The durable fix for long jobs
(video transcription holding a slot > 25s) is moving them to background jobs —
see the roadmap; not yet implemented.

## Required environment / secrets

| Var | Purpose |
| --- | --- |
| `FILTER_FYI_TRY_SECRET` | Shared secret with the Cloudflare Worker (must match) |
| `HEAVY_CONCURRENCY`, `EXECUTOR_WORKERS` | Capacity tuning (see Capacity & scaling) — safe defaults, raise after a VM bump |
| `FILTER_FYI_ADMIN_SECRET` | Shared secret for `/api/admin/*` — must match the Worker's `BOT_ADMIN_KEY`. Separate from `FILTER_FYI_TRY_SECRET` so the two can rotate independently |
| `TELEGRAM_TOKEN` | Telegram bot token (omit to run web-only) |
| `WEBHOOK_URL` | Public base URL for Telegram webhook mode |
| `TELEGRAM_WEBHOOK_SECRET` | Required in webhook mode — verifies inbound updates |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | LLM provider (Anthropic preferred) |
| `LITESTREAM_REPLICA_URL` (+ keys) | Enables continuous backup (optional but recommended) |
| `SCAN_ERRORS_ENABLED`, `GH_APP_*` | Daily error-log → GitHub-issue scanner |

## Runbook — Fly is wedged / dashboard unreachable

**Symptoms**

- `admin.filter.fyi/cost` shows `upstream unreachable`.
- `curl -i https://filter-fyi-backend.fly.dev/health` returns `503` from `server: Fly/...` (Fly's edge proxy) after a long delay (~30s).
- `fly status -a filter-fyi-backend` says machines are `started`, but the dashboard is dark.

The "machine started but app unresponsive" signature usually means the
uvicorn worker slots are blocked — the configured `hard_limit` of 40
concurrent requests in `fly.toml` is being soaked by long-running handlers,
including `/health`.

**Most common cause:** a Telegram retry storm. A handler chain (typically
video transcribe → summarise → analyse) takes longer than Telegram's ~60s
webhook timeout. Without the dedup + fire-and-forget in #34/#35, the same
update gets retried in parallel, each retry spawning another handler
chain, until every worker slot is held by a copy of the same work. After
those PRs landed this should not recur, but it remains the diagnostic to
rule out first.

**1. Diagnose**

```sh
fly logs -a filter-fyi-backend | tail -50         # what is the app doing?
fly checks list -a filter-fyi-backend             # consecutive /health failures?
fly machine status <id> -a filter-fyi-backend     # OOM kills? restarts?

# Has Telegram been retrying?
TOKEN=$(fly ssh console -a filter-fyi-backend -C "printenv TELEGRAM_TOKEN")
curl -sS "https://api.telegram.org/bot$TOKEN/getWebhookInfo" | jq '.result | {pending_update_count, last_error_message, last_error_date}'
```

A non-zero `pending_update_count` + a recent `last_error_date` is the
smoking gun for the retry storm pattern.

**2. Stop the bleed** (only needed if retries are still firing — i.e.
`pending_update_count > 0`)

```sh
# Drops queued Telegram updates so they don't all replay against the new
# code on restart.
curl -sS "https://api.telegram.org/bot$TOKEN/deleteWebhook?drop_pending_updates=true"
```

**3. Restart the machine**

Even after stopping new traffic, any handlers already mid-flight keep
running until they finish. Restarting cleanly kills them.

```sh
fly machine list -a filter-fyi-backend
fly machine restart <id> -a filter-fyi-backend
sleep 30
time curl -i https://filter-fyi-backend.fly.dev/health
# Expect 200 in <200ms.
```

**4. Re-enable the Telegram webhook**

If you ran step 2, the bot is offline until you re-register the webhook:

```sh
SECRET=$(fly ssh console -a filter-fyi-backend -C "printenv TELEGRAM_WEBHOOK_SECRET")
curl -sS -X POST "https://api.telegram.org/bot$TOKEN/setWebhook" \
  -d "url=https://filter-fyi-backend.fly.dev/webhook" \
  -d "secret_token=$SECRET"
curl -sS "https://api.telegram.org/bot$TOKEN/getWebhookInfo" | jq
```

**5. Quantify the damage**

The runaway spent real tokens. The admin dashboard's Cost tile shows
the day-level spike clearly; for an exact number:

```sh
fly ssh console -a filter-fyi-backend -C "sqlite3 /data/research.db \"\
  SELECT date(ts) day, COUNT(*) calls, printf('\$%.4f', SUM(cost_usd)) spent \
  FROM llm_calls \
  WHERE ts >= datetime('now', '-2 days') \
  GROUP BY day ORDER BY day\""
```

**6. Post-mortem checklist**

- Was there a recently merged change touching webhook routing, the handler
  chain, or `process_update`?
- Did a specific input (a long video, a YouTube livestream) trigger it?
- If yes, the input is probably worth adding to a fixture / fixture-like
  manual test next time the relevant code changes.

## Duplicate work — three layers of defense

If you see "the same content was processed N times" — for any reason —
walk these three caches/dedup layers in order. Each lives at a different
level, so the right diagnosis depends on *which* dimension is duplicated:

| Symptom | Layer | Where to look |
| --- | --- | --- |
| Same Telegram `update_id` arriving twice | `processed_updates` | `getWebhookInfo.pending_update_count`; Fly logs for `dropping duplicate webhook` |
| Same `(model, prompt, profile, content)` analysed twice | `llm_cache` | `SELECT COUNT(*) FROM llm_cache`; Fly logs for `cache hit (key=…)` |
| Same URL being re-fetched | `url_cache` | `SELECT url, fetched_at FROM url_cache WHERE url = ?` |

Cache hits and dedup drops are best-effort: a DB blip on the cache path
falls through to real work. None of them are correctness-critical — they
exist to make the same input cheap, not to prevent it.

The admin dashboard's Cost tile surfaces cache hit count, estimated cost
saved, and hit rate, so a quick check at `admin.filter.fyi/cost` is
usually the fastest way to spot a missing-cache regression after a
prompt/model change.
