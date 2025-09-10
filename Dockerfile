# Uses Chrome + Chromedriver preinstalled (works with Selenium)
FROM selenium/standalone-chromium:latest

USER root

# Install Python + Tailscale + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev gcc \
    tailscale curl ca-certificates jq procps iproute2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# App
COPY scraping.py /app/scraping.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Tailscale runtime env (userspace + SOCKS5)
ENV TS_SOCKET=/var/run/tailscale/tailscaled.sock
ENV TS_STATE=mem:
ENV TS_TUN=userspace-networking

# Run everything
CMD ["/entrypoint.sh"]
