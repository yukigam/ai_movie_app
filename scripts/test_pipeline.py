"""
Full pipeline test: TikTok series URL → Playwright extraction → ssstik
download → Supabase Storage + DB insert.

Mimics exactly what telegram_bot.py does when a user sends a series URL,
using a mock message object that logs instead of sending to Telegram.
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pipeline_test")

TEST_URL = "https://www.tiktok.com/@shortdramatime/video/7666143423493164308"
MAX_DOWNLOADS = 3  # download only first N episodes to keep test fast


class MockMessage:
    """Pretends to be a Telegram message; logs all edits."""

    def __init__(self, chat_id: str = "test"):
        self.chat_id = chat_id

    async def edit_text(self, text: str, **kwargs) -> None:
        log.info("[MSG] %s", text)

    async def reply_text(self, text: str, **kwargs) -> None:
        log.info("[MSG-REPLY] %s", text)


async def main() -> None:
    msg = MockMessage()

    # ── Step 1: extract episodes via Playwright (same code path as the bot) ──
    log.info("STEP 1: extracting episodes via Playwright...")
    episodes = await asyncio.wait_for(
        asyncio.to_thread(telegram_bot._extract_episodes_playwright, TEST_URL),
        timeout=600,
    )
    n_eps = sum(1 for e in episodes if isinstance(e, dict) and "_meta" not in e)
    log.info("Extraction returned %d episodes", n_eps)
    if n_eps <= 1:
        log.error("FAILED: expected >1 episode, got %d — cannot proceed", n_eps)
        return

    for e in episodes:
        if isinstance(e, dict) and "_meta" not in e:
            log.info("  EP %s -> %s", e["episode"], e["id"])

    # ── Step 2: download video sources for first N episodes ──────────────────
    log.info("STEP 2: downloading video sources for first %d episodes...", MAX_DOWNLOADS)
    videos = []
    for ep in episodes:
        if isinstance(ep, dict) and "_meta" in ep:
            continue
        if len(videos) >= MAX_DOWNLOADS:
            break
        ep_url = telegram_bot._clean_url(ep["url"])
        log.info("Fetching EP %s from %s ...", ep["episode"], ep_url)
        data = await asyncio.to_thread(telegram_bot._fetch_video, ep_url)
        if data and data.get("video_url"):
            data["title"] = f"Test Series EP.{ep['episode']}"
            videos.append(data)
            log.info("  Extractor OK for EP %s", ep["episode"])
        else:
            log.error("  All download methods FAILED for EP %s", ep["episode"])

    if not videos:
        log.error("FAILED: no video sources extracted")
        return

    # ── Step 3: insert into Supabase (same code path as the bot) ─────────────
    log.info("STEP 3: inserting %d videos into Supabase...", len(videos))
    await telegram_bot._insert(msg, videos)

    # ── Step 4: verify in the database ───────────────────────────────────────
    log.info("STEP 4: verifying rows in Supabase...")
    store = telegram_bot.Store()
    import re

    sid = telegram_bot.slug_id("Test Series")
    conn = store.db
    rows = conn.table("episodes").select("*").eq("series_id", sid).order("episode_number").execute()
    eps = rows.data or []
    log.info("VERIFY: %d episode rows for series '%s'", len(eps), sid)
    for r in eps:
        log.info("  EP %s: video=%s free=%s", r.get("episode_number"), r.get("video_url", "")[:60], r.get("is_free"))

    total_series = conn.table("series").select("id").eq("id", sid).execute()
    log.info("VERIFY: series row exists: %s", bool(total_series.data))

    if len(eps) >= 2:
        log.info("PASS: pipeline inserted %d episodes (>= 2) into Supabase", len(eps))
    else:
        log.error("FAIL: only %d episodes in DB", len(eps))


if __name__ == "__main__":
    asyncio.run(main())
