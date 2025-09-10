#!/bin/bash
set -e

echo "[INFO] Starting Container"

# --- DEBUG BLOCK ---
echo "[DEBUG] Printing all environment variables..."
printenv
echo "[DEBUG] Done printing environment variables."
# -------------------

# Check for Tailscale Auth Key
if [ -z "$TAILSCALE_AUTHKEY" ]; then
  echo "[ERROR] TAILSCALE_AUTHKEY is not set inside container!"
  exit 1
else
  echo "[INFO] TAILSCALE_AUTHKEY is present (length: ${#TAILSCALE_AUTHKEY})"
fi

# Start Tailscale in userspace networking mode
echo "[INFO] Starting Tailscale..."
/usr/sbin/tailscaled --state=mem: --socket=/tmp/tailscaled.sock &
sleep 5

/usr/bin/tailscale up \
  --authkey="${TAILSCALE_AUTHKEY}" \
  --hostname="railway-scraper" \
  --accept-routes \
  --accept-dns=false \
  --socket=/tmp/tailscaled.sock

if [ $? -ne 0 ]; then
  echo "[ERROR] Failed to bring up Tailscale!"
  exit 1
fi

echo "[INFO] Tailscale started successfully!"
tailscale ip -4
tailscale ip -6

# Run your Python scraper
echo "[INFO] Launching scraper..."
exec python3 /app/scraping.py

