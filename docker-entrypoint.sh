#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# Deployment entrypoint.
#
# The bot is 100% HTTP (httpx + yt-dlp) — no browser, no session storage.
# Cookies come from the platform secret (cookies.txt / TIKTOK_COOKIES_FILE).
# ─────────────────────────────────────────────────────────────────────────────
set -e

exec "$@"