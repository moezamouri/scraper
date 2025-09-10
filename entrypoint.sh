#!/bin/bash
set -euo pipefail

echo "[INFO] Starting Container"

# 1) Make sure the key is present
if [ -z "${TAILSCALE_AUTHKEY:-}" ]; then
  echo "[ERROR] TAILSCALE_AUTHKEY is not set inside container!"
  exit 1
else
  echo "[INFO] TAILSCALE_AUTHKEY detected (length: ${#TAILSCALE_AUTHKEY})"
fi

# 2) Start tailscaled in userspace, on a known socket, with SOCKS5 enabled
echo "[INFO] Starting tailscaled (userspace)…"
/usr/sbin/tailscaled \
  --tun=userspace-networking \
  --socks5-server=localhost:1055 \
  --state=mem: \
  --socket=/tmp/tailscaled.sock &
# Wait for the socket to appear
for i in {1..20}; do
  [ -S /tmp/tailscaled.sock ] && break
  sleep 0.25
done

# 3) Tell the CLI which socket to use (critical!)
export TS_SOCKET=/tmp/tailscaled.sock

# 4) Bring Tailscale up
echo "[INFO] Running 'tailscale up'…"
/usr/bin/tailscale up \
  --auth-key="${TAILSCALE_AUTHKEY}" \
  --hostname="railway-scraper" \
  --accept-routes \
  --accept-dns=false

echo "[INFO] ✅ Tailscale connected."
/usr/bin/tailscale status || true

# 5) Route Python traffic through Tailscale’s SOCKS5 proxy
export ALL_PROXY=socks5://127.0.0.1:1055
export NO_PROXY=localhost,127.0.0.1

# 6) Launch scraper
echo "[INFO] Launching scraper…"
exec python3 /app/scraping.py

