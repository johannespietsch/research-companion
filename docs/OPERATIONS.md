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

To enable, provision an S3-compatible bucket (Fly's Tigris, Backblaze B2,
Cloudflare R2, …) and set Fly secrets:

```sh
fly secrets set \
  LITESTREAM_REPLICA_URL="s3://<bucket>/filter-fyi-backend" \
  LITESTREAM_ACCESS_KEY_ID="<key>" \
  LITESTREAM_SECRET_ACCESS_KEY="<secret>"
# For non-AWS endpoints also set the endpoint, e.g. Tigris:
#   LITESTREAM_REPLICA_URL="s3://<bucket>/filter-fyi-backend?endpoint=https://fly.storage.tigris.dev"
```

Config: [`litestream.yml`](../litestream.yml) (30-day retention, daily
snapshots). On boot the entrypoint runs `litestream restore -if-db-not-exists`,
so a fresh/recovered volume self-heals from the replica.

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

Stored media (transcribed mp4s under `data/files/`) is the other growth vector;
files for deleted users are removed by account erasure, but monitor disk usage:
```sh
fly volumes list           # check % used
```

## Required environment / secrets

| Var | Purpose |
| --- | --- |
| `FILTER_FYI_TRY_SECRET` | Shared secret with the Cloudflare Worker (must match) |
| `TELEGRAM_TOKEN` | Telegram bot token (omit to run web-only) |
| `WEBHOOK_URL` | Public base URL for Telegram webhook mode |
| `TELEGRAM_WEBHOOK_SECRET` | Required in webhook mode — verifies inbound updates |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | LLM provider (Anthropic preferred) |
| `LITESTREAM_REPLICA_URL` (+ keys) | Enables continuous backup (optional but recommended) |
| `SCAN_ERRORS_ENABLED`, `GH_APP_*` | Daily error-log → GitHub-issue scanner |
