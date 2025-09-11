# Base has Chromium + chromedriver preinstalled
FROM selenium/standalone-chromium:latest

# We need root to install packages and run tailscaled
USER root

# Keep Python output unbuffered; keep image small
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install Python + curl/ca-certs, then install Tailscale (auto-detects Ubuntu/Debian)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip curl ca-certificates gnupg && \
    curl -fsSL https://tailscale.com/install.sh | sh && \
    rm -rf /var/lib/apt/lists/*

# App setup
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt
COPY . /app
RUN chmod +x /app/entrypoint.sh

# Optional: default NO_PROXY inside the container (Railway var can override)
# If you're not setting any global proxy, this doesn't really matter.
ENV NO_PROXY=localhost,127.0.0.1,solarweb.com,.solarweb.com

# Start tailscaled + bring the node up + run your scraper (handled in entrypoint.sh)
ENTRYPOINT ["/app/entrypoint.sh"]

