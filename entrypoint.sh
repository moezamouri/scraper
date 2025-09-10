#!/bin/sh
set -e

# Start Tailscale daemon
/usr/sbin/tailscaled --state=mem: --socket=/tmp/tailscaled.sock &
sleep 5

# Authenticate and join tailnet
/usr/bin/tailscale up --authkey=${TAILSCALE_AUTHKEY} --hostname=railway-scraper --accept-routes --accept-dns=false

# Run the scraper
exec python3 /app/scraping.py

