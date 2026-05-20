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

# Litestream — continuous SQLite backup to object storage. Only activated at
# runtime when LITESTREAM_REPLICA_URL is set (see docker-entrypoint.sh).
# TARGETARCH is auto-populated by BuildKit (amd64 on Fly, arm64 on Apple
# Silicon) and matches Litestream's release naming; the default keeps non-
# BuildKit builders on amd64 to mirror prod.
ARG LITESTREAM_VERSION=0.3.13
ARG TARGETARCH=amd64
RUN curl -fsSL "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-${TARGETARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin litestream

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium for the StreamYard interceptor.
# `--with-deps` runs its own apt-get install for the right system libs.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chmod +x docker-entrypoint.sh

# DATA_DIR is mounted at runtime (Fly volume). Default keeps local dev working.
ENV DATA_DIR=/data

EXPOSE 8080

# Entrypoint runs the app under Litestream when LITESTREAM_REPLICA_URL is set,
# otherwise plain uvicorn (identical to the previous CMD).
ENTRYPOINT ["./docker-entrypoint.sh"]
