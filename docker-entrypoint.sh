#!/bin/sh
# Entrypoint that optionally wraps the app in Litestream for continuous SQLite
# backup. If LITESTREAM_REPLICA_URL is unset (local dev, un-provisioned envs),
# we exec the app directly so behaviour is identical to before.
set -e

APP_CMD="uvicorn main:app --host 0.0.0.0 --port 8080"

# Tailscale egress for YouTube (#128). Userspace tailscaled exposes a SOCKS5
# proxy on localhost:1055 whose traffic exits via TS_EXIT_NODE (the home
# MacBook); nothing else on the machine is routed through it. State lives on
# the volume so the node keeps its identity across deploys and TS_AUTHKEY is
# only consumed on first login. Runs in the background and never blocks or
# fails the boot: if the tailnet is down, YouTube fetches fall back to direct.
if [ -n "$TS_AUTHKEY" ]; then
  TS_STATE_DIR="${DATA_DIR:-/data}/tailscale"
  TS_SOCKET=/tmp/tailscaled.sock
  mkdir -p "$TS_STATE_DIR"
  # The app sends YouTube traffic here (bot.config.YOUTUBE_PROXY); only set
  # when tailscaled actually runs, so without a key fetches record 'direct'.
  export YOUTUBE_PROXY="${YOUTUBE_PROXY:-socks5h://localhost:1055}"
  echo "[entrypoint] starting tailscaled (userspace, SOCKS5 localhost:1055)"
  tailscaled --tun=userspace-networking --socks5-server=localhost:1055 \
    --state="$TS_STATE_DIR/tailscaled.state" --socket="$TS_SOCKET" \
    >/tmp/tailscaled.log 2>&1 &
  (
    sleep 2
    # Two steps: `up --exit-node=<hostname>` fails before login ("cannot
    # resolve exit node by hostname while Tailscale is starting up"), so log
    # in first, then set the exit node by name once peers are known. Errors
    # are logged with the key redacted.
    redact() { sed 's/tskey-[A-Za-z0-9-]*/tskey-REDACTED/g'; }
    if out=$(tailscale --socket="$TS_SOCKET" up --reset --auth-key="$TS_AUTHKEY" \
        --hostname="${TS_HOSTNAME:-filter-fyi-backend}" --advertise-tags=tag:fly \
        --timeout=60s 2>&1); then
      echo "[entrypoint] tailscale up"
    else
      echo "[entrypoint] tailscale up failed — YouTube will go direct: $(echo "$out" | redact)"
      exit 0
    fi
    [ -n "$TS_EXIT_NODE" ] || exit 0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if out=$(tailscale --socket="$TS_SOCKET" set --exit-node="$TS_EXIT_NODE" 2>&1); then
        echo "[entrypoint] tailscale exit node: $TS_EXIT_NODE"
        exit 0
      fi
      sleep 3
    done
    echo "[entrypoint] setting exit node $TS_EXIT_NODE failed — YouTube will go direct: $(echo "$out" | redact)"
  ) &
else
  echo "[entrypoint] TS_AUTHKEY unset — starting without Tailscale egress"
fi

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
