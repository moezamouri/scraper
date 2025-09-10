USER root

# Python + Tailscale repo + Tailscale
RUN apt-get update && \
    apt-get install -y curl gnupg python3 python3-pip && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list | tee /etc/apt/sources.list.d/tailscale.list && \
    apt-get update && \
    apt-get install -y tailscale && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip3 install --no-cache-dir -r requirements.txt

# Make tailscale CLI talk to the same socket the daemon will use
ENV TS_SOCKET=/var/run/tailscale/tailscaled.sock

# Optional global proxy; don't force Chrome proxy (script controls via PROXY_URL)
# Keep ALL_PROXY unset by default; user can enable if needed at deploy time
# ENV ALL_PROXY=socks5://127.0.0.1:1055

# Never proxy local and solarweb domains (bypass proxy if ALL_PROXY is set externally)
ENV NO_PROXY=localhost,127.0.0.1,solarweb.com,.solarweb.com

# One-shot startup: start tailscaled (userspace), wait for socket, tailscale up, then run scraper
CMD bash -lc '\
  set -e; \
  if [ -z "${TAILSCALE_AUTHKEY:-}" ]; then echo "[ERROR] TAILSCALE_AUTHKEY missing"; exit 1; fi; \
  mkdir -p /var/run/tailscale; \
  /usr/sbin/tailscaled --tun=userspace-networking --socks5-server=localhost:1055 --state=mem: --socket="$TS_SOCKET" & \
  for i in {1..40}; do [ -S "$TS_SOCKET" ] && break; sleep 0.25; done; \
  if [ ! -S "$TS_SOCKET" ]; then echo "[ERROR] tailscaled socket not found at $TS_SOCKET"; exit 1; fi; \
  tailscale up --auth-key="${TAILSCALE_AUTHKEY}" --hostname="railway-scraper" --accept-routes --accept-dns=false; \
  tailscale status || true; \
  exec python3 /app/scraping.py \
'
