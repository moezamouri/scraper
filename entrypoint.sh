#!/usr/bin/env bash
set -euo pipefail

# ---- Start tailscaled in userspace with local SOCKS5 on 1055, memory state ----
/usr/sbin/tailscaled \
  --tun=userspace-networking \
  --socks5-server=localhost:1055 \
  --state=mem: \
  --verbose=1 &

# Wait for tailscaled to accept CLI
sleep 2

# ---- Authenticate into your tailnet (ephemeral auth key) ----
tailscale up \
  --authkey="${TAILSCALE_AUTHKEY}" \
  --hostname="railway-scraper" \
  --accept-dns=false \
  --ssh=false

# Wait until we actually have any peers listed (means we joined the tailnet)
for i in {1..30}; do
  if tailscale status --peers 2>/dev/null | grep -q .; then
    echo "[ok] Tailscale connected."
    break
  fi
  echo "[..] Waiting for Tailscale to connect..."
  sleep 2
done

# ---- Optional: quick HA preflight over SOCKS to catch auth/network issues early ----
if command -v curl >/dev/null 2>&1; then
  echo "[..] Probing Home Assistant API via SOCKS..."
  if ! curl -sS --max-time 5 --socks5-hostname 127.0.0.1:1055 \
       -H "Authorization: Bearer ${HA_TOKEN}" \
       "${HA_URL}/api/" >/dev/null ; then
    echo "[warn] Preflight to HA failed (might still be booting). Continuing..."
  else
    echo "[ok] HA preflight succeeded."
  fi
fi

# ---- Run your scraper ----
exec python3 /app/scraping.py

