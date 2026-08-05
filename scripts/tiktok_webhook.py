#!/usr/bin/env python3
"""
FastAPI webhook — TikTok scrape + parse → n8n consumes the result.

Endpoints
---------
POST /scrape
    { "url": "https://www.tiktok.com/@user/video/123" }
    → { "ok": true, "data": { "title", "description", "video_url",
        "thumbnail", "username", "series_title", "series_slug",
        "episode_number", "duration", "webpage_url" } }

POST /scrape-batch
    { "username": "shortdramatime" }
    → { "ok": true, "data": [ { same shape as above }, ... ] }

Run
---
    uvicorn tiktok_webhook:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import os
import re
import sys

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("ttwebhook")

# ── Import shared logic from telegram_bot ────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
import telegram_bot  # noqa: E402  (isort:skip)

fetch_video = telegram_bot._fetch_video
fetch_entries = telegram_bot._fetch_entries
parse_episode = telegram_bot.parse_episode
slug_id = telegram_bot.slug_id
resolve_short_url = telegram_bot.resolve_short_url
is_short_link = telegram_bot.is_short_link
clean_caption = telegram_bot.clean_caption
is_garbage_title = telegram_bot.is_garbage_title
SUPABASE_URL = telegram_bot.SUPABASE_URL
SUPABASE_KEY = telegram_bot.SUPABASE_KEY
playwright_available = telegram_bot._playwright_available
extract_episodes_playwright = telegram_bot._extract_episodes_playwright

# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="TikTok Scraper Webhook", version="1.0.0")


class ScrapeRequest(BaseModel):
    url: str


class BatchRequest(BaseModel):
    username: str


class ScrapeResponse(BaseModel):
    ok: bool
    data: dict | None = None
    error: str | None = None


class BatchResponse(BaseModel):
    ok: bool
    data: list[dict] = []
    total: int = 0
    error: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _enrich(v: dict) -> dict:
    """Add parsed series/episode fields to a scraped video dict."""
    series_title, ep_num = parse_episode(v.get("title", ""))
    if is_garbage_title(series_title):
        author = v.get("username", "") or v.get("uploader", "")
        if author:
            series_title = f"@{author}"
    return {
        "webpage_url": v.get("webpage_url", ""),
        "title": v.get("title", ""),
        "description": v.get("description", ""),
        "video_url": v.get("video_url", ""),
        "thumbnail": v.get("thumbnail", ""),
        "duration": v.get("duration", 1),
        "username": v.get("username", ""),
        "series_title": series_title,
        "series_slug": slug_id(series_title),
        "episode_number": ep_num,
    }


def _normalise_url(raw: str) -> str | None:
    """Resolve short links and return a full TikTok URL, or None."""
    url = raw.strip()
    if is_short_link(url):
        resolved = resolve_short_url(url)
        if not resolved:
            return None
        url = resolved
    if "tiktok.com/@" not in url:
        return None
    return url


# ── Endpoints ────────────────────────────────────────────────────────────────


@app.post("/scrape", response_model=ScrapeResponse)
def scrape(req: ScrapeRequest):
    url = _normalise_url(req.url)
    if not url:
        raise HTTPException(400, "Invalid or unresolvable TikTok URL")

    result = fetch_video(url)
    if not result or not result.get("video_url"):
        raise HTTPException(502, "Failed to extract video from ssstik.io / yt-dlp")

    enriched = _enrich(result)
    log.info("Scraped: %s → '%s' EP.%s", url, enriched["series_title"], enriched["episode_number"])
    return {"ok": True, "data": enriched}


@app.post("/scrape-batch", response_model=BatchResponse)
def scrape_batch(req: BatchRequest):
    username = req.username.strip().lstrip("@")
    entries = fetch_entries(username)
    if not entries:
        return {"ok": True, "data": [], "total": 0}

    urls = []
    for e in entries[:50]:
        uid = e.get("id") or e.get("url", "")
        if not uid:
            continue
        urls.append(
            f"https://www.tiktok.com/@{username}/video/{uid}"
            if not uid.startswith("http")
            else uid
        )

    videos = []
    for i in range(0, len(urls), 5):
        batch = urls[i : i + 5]
        for u in batch:
            result = fetch_video(u)
            if result and result.get("video_url"):
                videos.append(_enrich(result))

    log.info("Batch scraped @%s: %d/%d videos", username, len(videos), len(urls))
    return {"ok": True, "data": videos, "total": len(videos)}


@app.post("/scrape-series")
def scrape_series(req: ScrapeRequest):
    """Extract all episodes from a TikTok series/playlist via Playwright."""
    if not playwright_available():
        raise HTTPException(400, "Playwright session not found. Run login_tiktok.py first.")

    url = _normalise_url(req.url)
    if not url:
        raise HTTPException(400, "Invalid or unresolvable TikTok URL")

    episodes = extract_episodes_playwright(url)
    if not episodes:
        raise HTTPException(404, "No episodes found in this series")

    videos = []
    for ep in episodes:
        result = fetch_video(ep["url"])
        if result and result.get("video_url"):
            videos.append(_enrich(result))

    log.info("Series scraped %s → %d/%d episodes", url, len(videos), len(episodes))
    return {"ok": True, "data": videos, "total": len(videos)}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("WEBHOOK_PORT", "8000"))
    uvicorn.run("tiktok_webhook:app", host="0.0.0.0", port=port, reload=False)
