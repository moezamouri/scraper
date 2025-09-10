# Use Selenium’s official Chrome + WebDriver image
FROM selenium/standalone-chromium:latest

# Install Python
USER root
RUN apt-get update && apt-get install -y python3 python3-pip

# Set working directory
WORKDIR /app

# Copy project files
COPY . /app

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Run the scraper
CMD ["python3", "scraping.py"]
