# Expo HAS CHANGED

Read the exact versioned docs at https://docs.expo.dev/versions/v57.0.0/ before writing any code.

# Populate Script

Use `scripts/populate_from_tiktok.py` to populate Supabase from TikTok URLs.

Requirements: `pip install -r scripts/requirements.txt`
Key: Add `SUPABASE_SERVICE_ROLE_KEY` to `.env` (from Supabase Dashboard > Project Settings > API)

Usage: `python scripts/populate_from_tiktok.py <tiktok_url> --series-id <id> --episode <N> [--free]`

# Telegram Bot

A Telegram bot (`scripts/telegram_bot.py`) that accepts TikTok usernames or video URLs
and inserts videos into Supabase series/episodes tables.

Setup:
1. `pip install -r scripts/requirements.txt`
2. Add to `.env`:
   - `TELEGRAM_BOT_TOKEN` — from https://t.me/BotFather
   - `SUPABASE_SERVICE_ROLE_KEY` — from Supabase Dashboard > Project Settings > API
3. **One-time login** (required for series extraction):
   `python scripts/login_tiktok.py`
   → Opens a browser; log in to TikTok manually, then close it.
4. Run: `python scripts/telegram_bot.py`

Commands:
- Send any TikTok **video URL** → auto-detects series episodes (via Playwright) and imports them
- `/series <url>` → explicitly extract episodes from a series
- `/login` → re-authenticate TikTok session
- Send `@username` or profile URL → fetches all public videos (up to 50)
- Send single TikTok URL → fetches one video (short links auto-resolved)

How it works:
- **Series extraction**: When a user sends a TikTok video URL, Playwright opens the page,
  finds the "Episodes" sidebar, clicks each episode button, and collects all video URLs.
  Each URL is then passed through ssstik.io downloader → Supabase Storage → tables.
- **Single video**: ssstik.io (free downloader, no API key needed) + yt-dlp fallback
- **Profile**: yt-dlp → web scraping → RapidAPI fallback chain
- Titles like "Perfect Timing EP.1" are auto-parsed into series + episode number
- First 2 episodes marked free, rest are locked
- Duplicate detection via upsert (safe to re-run)

# Playwright Persistent Context

The series extraction uses `scripts/tiktok_playwright.py` which manages a persistent
Playwright session. The storage state (`playwright_storage.json`) saves cookies + localStorage
so TikTok recognises the session as logged-in across bot restarts.

Architecture:
- `scripts/tiktok_playwright.py` — core module: `extract_episodes_sync(url)` returns episode list
- `scripts/login_tiktok.py` — one-time login helper (calls `tiktok_playwright._do_login()`)
- Storage file: `playwright_storage.json` (auto-created after login, refreshed on each extraction)

# Pipeline Test

`python scripts/test_pipeline.py` runs the full bot code path end-to-end
(extract 70 eps → download first 3 → Supabase insert → DB verify).

Notes:
- Extraction of 70 episodes takes ~4 min (per-click jitter) — timeouts are 600s
- TikTok rate-limits after a few runs: if CAPTCHA appears, re-run
  `python scripts/login_tiktok.py` first, then run the test ONCE
- Bot extracts episodes by clicking sidebar items with `page.mouse.click`
  (JS `.click()` doesn't trigger React) and waits for tab content via
  `_wait_for_tab_content` (networkidle + DOM marker change)

# Render Docker session (CAPTCHA fix)

A container has NO `playwright_storage.json` (gitignored, `.dockerignore`'d) → the
bot runs headless with an **anonymous** session → TikTok serves a CAPTCHA puzzle
and the episode sidebar never renders → *only 1 episode is extracted*.

To give Render a logged-in session **without committing cookies to the public
repo**, the bot reads a Netscape-format `cookies.txt`:

- `tiktok_playwright.py` `_load_storage_state()` → prefers
  `playwright_storage.json`, else builds `storageState` from the first
  existing file of: `$TIKTOK_COOKIES_FILE`, `cookies.txt` (project root),
  `/app/cookies.txt`, `/etc/secret_files/cookies.txt`, `/etc/secrets/cookies.txt`
- Generate the seed file locally (37 cookies):
  `python scripts/diag_sidebar.py` is NOT that — use a storage→cookies dump:
  `python -c "import json; d=json.load(open('playwright_storage.json')); print(chr(10).join(f'.\tTRUE\t/\t{("TRUE" if c.get("secure") else "FALSE")}\t{c.get("expires") or 1893456000}\t{c["name"]}\t{c["value"]}' for c in d.get('cookies',[]) if 'tiktok' in c.get('domain','') or 'byte' in c.get('domain','')) )" > cookies.txt`
  (or run the existing _ensure_cookies() conversion in telegram_bot.py)

Render setup:
1. Render Dashboard → service → Environment → **Secret Files** → add
   file `cookies.txt` whose content = `scripts/../cookies.txt` contents
2. (optional) set `TIKTOK_COOKIES_FILE` env if mounted elsewhere

In-page CAPTCHAs are transient: `extract_episodes()` retries 6× with
20–60 s backoff before giving up (the sidebar is read only after the wall
clears). Social‑flow cues: recent-ranking: same as series extraction above.
