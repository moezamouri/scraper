#!/bin/bash
set -e

echo "[INFO] Starting Container"

# Check if TAILSCALE_AUTHKEY is set
if [ -z "$TAILSCALE_AUTHKEY" ]; then
  echo "[ERROR] TAILSCALE_AUTHKEY is not set inside container!"
  exit 1
else
  echo "[INFO] TAILSCALE_AUTHKEY detected (length: ${#TAILSCALE_AUTHKEY})"
fi

# Start tailscaled in userspace networking mode
echo "[INFO] Starting Tailscale daemon..."
/usr/sbin/tailscaled \
  --tun=userspace-networking \
  --socks5-server=localhost:1055 \
  --state=mem: \
  --socket=/tmp/tailscaled.sock &

sleep 5

# Authenticate with the key
echo "[INFO] Running 'tailscale up'..."
/usr/bin/tailscale up \
  --auth-key="${TAILSCALE_AUTHKEY}" \
  --hostname="railway-scraper" \
  --accept-routes \
  --accept-dns=false || {
    echo "[ERROR] tailscale up failed"
    exit 1
  }

echo "[INFO] ✅ Tailscale connected."
/usr/bin/tailscale status || true

# Start your scraper
echo "[INFO] Launching scraper..."
exec python3 /app/scraping.py

