#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# Deployment entrypoint.
#
# The compose file bind-mounts ./playwright_storage.json.  On the very first
# `docker compose up` with no local file, Docker creates an empty DIRECTORY
# at the mount point — which would make the bot crash when it tries to save
# its TikTok session there.  Turn it back into a real (empty) file, then run
# the real command.
# ─────────────────────────────────────────────────────────────────────────────
set -e

if [ -d /app/playwright_storage.json ]; then
    rm -rf /app/playwright_storage.json
fi
if [ ! -f /app/playwright_storage.json ]; then
    touch /app/playwright_storage.json
fi

exec "$@"
