FROM selenium/standalone-chromium:latest

USER root

# Install Python + Tailscale
RUN apt-get update && \
    apt-get install -y python3 python3-pip tailscale && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Run entrypoint
CMD ["/app/entrypoint.sh"]
