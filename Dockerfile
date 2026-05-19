FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps:
#   ffmpeg     — required by faster-whisper for audio/video decoding
#   nodejs     — JS runtime yt-dlp uses to solve YouTube's signature challenges
#                (without one, yt-dlp falls back to a deprecated path that 429s
#                much more aggressively)
#   ca-certs   — outbound TLS to Telegram / Anthropic / etc.
#   curl       — useful for healthcheck debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      nodejs \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium for the StreamYard interceptor.
# `--with-deps` runs its own apt-get install for the right system libs.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# DATA_DIR is mounted at runtime (Fly volume). Default keeps local dev working.
ENV DATA_DIR=/data

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
