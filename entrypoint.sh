#!/usr/bin/env bash
set -euo pipefail

echo "[init] starting tailscaled…"
/usr/sbin/tailscaled \
  --tun="${TS_TUN:-userspace-networking}" \
  --socks5-server=localhost:1055 \
  --state="${TS_STATE:-mem:}" \
  --socket="${TS_SOCKET:-/var/run/tailscale/tailscaled.sock}" &

# wait for tailscaled to be ready for CLI
for i in {1..30}; do
  if tailscale status >/dev/null 2>&1; then break; fi
  sleep 0.3
done

if [[ -z "${TAILSCALE_AUTHKEY:-}" ]]; then
  echo "[fatal] TAILSCALE_AUTHKEY is not set"; exit 1
fi

echo "[init] tailscale up…"
tailscale up \
  --authkey="${TAILSCALE_AUTHKEY}" \
  --hostname="${TS_HOSTNAME:-railway-scraper}" \
  --accept-routes \
  --accept-dns=false \
  --ssh=false

echo "[info] my TS IPv4:"
tailscale ip -4 || true

# Route all HTTP(S) via Tailscale SOCKS5 (userspace networking)
export ALL_PROXY="socks5h://localhost:1055"
export HTTPS_PROXY="$ALL_PROXY"
export HTTP_PROXY="$ALL_PROXY"
export NO_PROXY="127.0.0.1,localhost"

# Optional: quick HA API probe if envs are present
if [[ -n "${HA_BASE_URL:-}" && -n "${HA_TOKEN:-}" ]]; then
  echo "[check] probing Home Assistant at $HA_BASE_URL"
  if curl -sSf -H "Authorization: Bearer ${HA_TOKEN}" "${HA_BASE_URL}/api/" >/dev/null; then
    echo "[ok] HA reachable."
  else
    echo "[warn] cannot reach HA at ${HA_BASE_URL} (continuing anyway)."
  fi
fi

echo "[run] python scraping.py"
exec python3 /app/scraping.py

