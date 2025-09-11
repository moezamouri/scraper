FROM selenium/standalone-chromium:latest
USER root

# Install Python tooling (if not already present) + Tailscale
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates python3 python3-pip && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.gpg | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.list | tee /etc/apt/sources.list.d/tailscale.list >/dev/null && \
    apt-get update && apt-get install -y --no-install-recommends tailscale && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt
COPY . /app

# Avoid any platform proxies for these hosts (belt & suspenders)
ENV NO_PROXY=localhost,127.0.0.1,solarweb.com,.solarweb.com

# Use our robust entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]

