# ─────────────────────────────────────────────────────────────────────────────
# 24/7 Cloud deployment for the TikTok → Supabase Telegram bot.
#
# Base image: the official Playwright image — ships Python, Chromium and all
# system libraries needed to run headless browsers on a minimal container
# (no apt-get, no Xvfb required).  Pin the tag to the Playwright version in
# requirements.txt (v1.61.0).
# ─────────────────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

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
