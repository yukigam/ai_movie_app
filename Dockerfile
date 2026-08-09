# ─────────────────────────────────────────────────────────────────────────────
# 24/7 Cloud deployment for the TikTok → Supabase Telegram bot.
#
# Pure-HTTP arch (httpx + yt-dlp): NO Chromium, NO Playwright — the whole
# pipeline runs at ~15 MB RAM, far below the Render Free 512 MB limit.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Logs go to stdout/stderr (docker logs); no bytecode cache; UTC clock.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

# Install Python dependencies first — layer stays cached unless the
# requirements change.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the project (secrets like .env are excluded via .dockerignore).
COPY . .

RUN chmod +x /app/docker-entrypoint.sh

# The bot reads .env from the project root (python-dotenv) and connects to
# Telegram + Supabase using the variables injected by docker-compose.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "scripts/telegram_bot.py"]