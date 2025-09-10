# Selenium + Chrome (works with your Selenium code)
FROM selenium/standalone-chromium:latest

USER root

# Base tools (curl needed for Tailscale installer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev gcc \
    curl ca-certificates jq procps iproute2 \
 && rm -rf /var/lib/apt/lists/*

# Install Tailscale via official script (adds repo + installs package)
RUN curl -fsSL https://tailscale.com/install.sh | sh

WORKDIR /app

# Python deps
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# App code + entrypoint
COPY scraping.py /app/scraping.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Tailscale runtime config
ENV TS_SOCKET=/var/run/tailscale/tailscaled.sock
ENV TS_STATE=mem:
ENV TS_TUN=userspace-networking
ENV PYTHONUNBUFFERED=1

CMD ["/entrypoint.sh"]
