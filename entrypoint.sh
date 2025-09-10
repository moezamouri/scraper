#!/bin/bash
set -e

echo "[INFO] Starting Tailscale (userspace mode)..."

# Start tailscaled in userspace mode (no /dev/net/tun required)
tailscaled --state=mem: --socket=/tmp/tailscaled.sock --tun=userspace-networking &
sleep 5

# Bring Tailscale up with your auth key
tailscale --socket=/tmp/tailscaled.sock up \
  --authkey=${TAILSCALE_AUTHKEY} \
  --hostname=scraper-railway \
  --accept-dns=false

echo "[INFO] Tailscale started successfully!"

# Route all outbound traffic through Tailscale's SOCKS5 proxy
export ALL_PROXY=socks5://127.0.0.1:1055
export NO_PROXY=localhost,127.0.0.1

# Debug: show Tailscale IP
tailscale --socket=/tmp/tailscaled.sock ip -4 || true

echo "[INFO] Launching scraper..."
exec python3 /app/scraping.py

