#!/bin/sh
# Entrypoint that optionally wraps the app in Litestream for continuous SQLite
# backup. If LITESTREAM_REPLICA_URL is unset (local dev, un-provisioned envs),
# we exec the app directly so behaviour is identical to before.
set -e

APP_CMD="uvicorn main:app --host 0.0.0.0 --port 8080"

if [ -z "$LITESTREAM_REPLICA_URL" ]; then
  echo "[entrypoint] LITESTREAM_REPLICA_URL unset — starting without backup"
  exec $APP_CMD
fi

CONFIG=/app/litestream.yml
DB_PATH="${DATA_DIR:-/data}/research.db"

# Restore from the replica if this volume has no database yet (fresh machine or
# recovered volume). No-op when a replica doesn't exist; never clobbers a DB
# that's already present (-if-db-not-exists).
echo "[entrypoint] attempting Litestream restore (if needed) for $DB_PATH"
litestream restore -config "$CONFIG" -if-db-not-exists -if-replica-exists "$DB_PATH" || \
  echo "[entrypoint] restore skipped/failed (continuing)"

echo "[entrypoint] starting app under Litestream replication"
exec litestream replicate -config "$CONFIG" -exec "$APP_CMD"
