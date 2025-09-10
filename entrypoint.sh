#!/bin/bash
set -e

# --- Debug: Check if secrets are present ---
if [ -z "$TAILSCALE_AUTHKEY" ]; then
  echo "[ERROR] TAILSCALE_AUTHKEY is not set inside container!"
  exit 1
else
  echo "[INFO] TAILSCALE_AUTHKEY is present (length: ${#TAILSCALE_AUTHKEY})"
fi

if [ -z "$HA_TOKEN" ]; then
  echo "[ERROR] HA_TOKEN is not set inside container!"
  exit 1
else
  echo "[INFO] HA_TOKEN is present (length: ${#HA_TOKEN})"
fi

# --- Start Tailscale in userspace mode (no /dev/net/tun required) ---
echo "[INFO] Starting Tailscale (userspace mode)..."
tailscaled --state=mem: --socket=/tmp/tailscaled.sock --tun=userspace-networking &
sleep 5

# --- Authenticate Tailscale using auth key ---
tailscale --socket=/tmp/tailscaled.sock up \
  --authkey=${TAILSCALE_AUTHKEY} \
  --hostname=scraper-railway \
  --accept-dns=false

echo "[INFO] Tailscale started successfully!"

# --- Debug: Show Tailscale IP ---
tailscale --socket=/tmp/tailscaled.sock ip -4 || true

# --- Route all outbound traffic through Tailscale's SOCKS proxy ---
export ALL_PROXY=socks5://127.0.0.1:1055
export NO_PROXY=localhost,127.0.0.1

# --- Run scraper ---
echo "[INFO] Launching scraper..."
exec python3 /app/scraping.py

