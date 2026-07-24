#!/usr/bin/env bash
# Start co-located RSSHub (localhost:1200) + silph-relay (health on $PORT).
set -euo pipefail

RSSHUB_PORT="${RSSHUB_PORT:-1200}"
CACHE_EXPIRE="${CACHE_EXPIRE:-30}"
export RSSHUB_URL="${RSSHUB_URL:-http://127.0.0.1:${RSSHUB_PORT}}"

echo "[start] RSSHUB_URL=${RSSHUB_URL}"
echo "[start] CACHE_EXPIRE=${CACHE_EXPIRE}"
echo "[start] Public PORT=${PORT:-10000}"

if [ -z "${TWITTER_AUTH_TOKEN:-}" ]; then
  echo "[start] WARNING: TWITTER_AUTH_TOKEN is not set — RSSHub's twitter routes"
  echo "[start]          will fail. Set it to a burner account's auth_token cookie."
fi

# ── RSSHub lives in the base image at /app ───────────────────────────────────
cd /app
PORT="${RSSHUB_PORT}" \
CACHE_EXPIRE="${CACHE_EXPIRE}" \
NODE_ENV=production \
  npm run start &
RSSHUB_PID=$!
echo "[start] RSSHub pid=${RSSHUB_PID} on :${RSSHUB_PORT}"

# Wait until RSSHub responds (or give up after ~90s)
for i in $(seq 1 45); do
  if curl -sf "http://127.0.0.1:${RSSHUB_PORT}" >/dev/null 2>&1; then
    echo "[start] RSSHub is up"
    break
  fi
  if ! kill -0 "${RSSHUB_PID}" 2>/dev/null; then
    echo "[start] RSSHub exited early — check build/logs"
    exit 1
  fi
  sleep 2
done

# ── Python relay (health server binds Render $PORT) ──────────────────────────
cd /relay
export RSSHUB_URL
exec python3 src/main.py
