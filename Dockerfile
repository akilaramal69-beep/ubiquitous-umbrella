FROM python:3.11-slim

# Install system dependencies including Playwright requirements
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    git \
    gcc \
    python3-dev \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    fonts-unifont \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium || true

# Copy project files
COPY . .

# Create downloads directory
RUN mkdir -p DOWNLOADS

# Set ffmpeg path explicitly so yt-dlp always finds it
ENV FFMPEG_PATH=/usr/bin/ffmpeg

# Koyeb expects a web service to listen on port 8080
EXPOSE 8080

# Start the bot (Flask health server runs in a background thread inside bot.py)
CMD ["python3", "bot.py"]
