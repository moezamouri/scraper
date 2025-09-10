FROM selenium/standalone-chromium:latest

USER root

# Install Python + add Tailscale repo + install Tailscale
RUN apt-get update && \
    apt-get install -y curl gnupg python3 python3-pip && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list | tee /etc/apt/sources.list.d/tailscale.list && \
    apt-get update && \
    apt-get install -y tailscale && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Install Python deps
RUN pip3 install --no-cache-dir -r requirements.txt

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
