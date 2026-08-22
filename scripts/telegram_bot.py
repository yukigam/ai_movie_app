#!/usr/bin/env python3
"""
Telegram Bot — TikTok Drama Series → Supabase.

Extracts TikTok videos (single URL or full profile) and inserts them into
Supabase series/episodes tables. Episode numbers are auto-parsed from
titles matching patterns like "Title EP.1", "Title - Episode 2", "Part 3".

Requires:
    pip install -r scripts/requirements.txt

Environment variables (.env):
    TELEGRAM_BOT_TOKEN          — from @BotFather
    EXPO_PUBLIC_SUPABASE_URL    — already in .env
    SUPABASE_SERVICE_ROLE_KEY   — add from Supabase Dashboard > Settings > API
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import sys
import time

import gc
import tempfile
import threading

from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import Update
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
# Mirror logs to a file so the bot stays debuggable when run detached
# (e.g. pythonw.exe, where stderr does not exist).
try:
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_bot.log")
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception:
    pass
log = logging.getLogger("ttbot")

# ── Render keep-alive HTTP server (deployment only, no bot logic) ────────────
# Render kills a Web Service if its port stays silent, and a polling bot never
# accepts connections.  The health server runs in a daemon THREAD and the
# bot's asyncio event loop stays in the MAIN thread — Python 3.12 forbids
# creating an event loop in a background thread (`set_wakeup_fd only works in
# main thread`), so the polling must never leave the main thread.  It starts
# ONLY when this file is run as a script (python scripts/telegram_bot.py);
# module imports (test_pipeline, tiktok_webhook, clean_and_redownload) are
# unaffected.
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802  (stdlib naming)
        self._answer()

    def do_HEAD(self) -> None:  # noqa: N802  (probes also use HEAD)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.end_headers()

    def _answer(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args: object) -> None:
        pass


def start_health_server() -> None:
    """Serve the Render keep-alive 200 on 0.0.0.0:$PORT (default 10000) from
    a daemon thread.  Returns immediately; the bot owns the main thread."""
    port = int(os.environ.get("PORT", "10000"))
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    except Exception as e:
        log.warning("! Health server on port %s failed to start: %s", port, e)
        return
    threading.Thread(
        target=server.serve_forever, daemon=True, name="render-health"
    ).start()
    log.info("✓ Render health server listening on 0.0.0.0:%s", port)


if __name__ == "__main__":
    start_health_server()

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
VALID_GENRES = {"Sci-Fi", "Fantasy", "Romance", "Horror"}
DEFAULT_GENRE = "Sci-Fi"

DEFAULT_FREE_FIRST = 2

# Hard cap per episode for the video-source/download step.  If a single
# episode takes longer than this, it is skipped and the import continues
# with the next episode.
EPISODE_FETCH_TIMEOUT = 20.0

# Watchdog: an episode's source fetch that shows NO progress within this
# many seconds is force-skipped (thread abandoned) and the import moves on.
# This is what stops a stalled TikTok/ssstik window from hanging the loop.
FETCH_WATCHDOG = 10.0

# Outer safety nets for DB calls and video uploads (the Supabase client
# also carries its own 30s timeouts — these are the final line of defense).
DB_WATCHDOG = 40.0
UPLOAD_WATCHDOG = 150.0

# Parallel upload pipeline: how many episodes are downloaded from the CDN
# and pushed to Supabase Storage AT THE SAME TIME inside one import.
# The old sequential loop spent 30-60s per episode (download + upload +
# thumbnail + DB write, one after another) → a 50-episode series crawled
# for ~30 min.  6 workers ≈ 50 episodes in 3-5 minutes; RAM stays bounded
# because each worker holds at most ONE video's bytes (~10 MB).
IMPORT_WORKERS = int(os.getenv("IMPORT_WORKERS", "6"))

# Deployed-commit marker: Render injects RENDER_GIT_COMMIT at build time.
# `/version` in Telegram reveals exactly which code the service runs —
# no Dashboard access needed for remote diagnosis.
BOT_VERSION = (os.getenv("RENDER_GIT_COMMIT") or "local-dev")[:7]

# Retry-pass cap: per-episode timeout escalates 20s → 40s → 80s → 120s and
# then stays at this value until the episode downloads successfully.
MAX_RETRY_TIMEOUT = 120.0

# Refuse to upload video files smaller than this — a real TikTok clip is
# multiple MB; anything tiny is a 0-byte upload, an error page or an
# aborted download.
MIN_VIDEO_SIZE = 500 * 1024

# Strict automated retry caps.  A series is only reported as "complete"
# after EVERY episode downloaded, uploaded and verified healthy.
MAX_FETCH_ATTEMPTS = 5   # re-fetch rounds for a failed episode (full fallback chain each round)
MAX_UPLOAD_ATTEMPTS = 5  # re-download+re-upload rounds inside _insert

# Instant re-sync attempts per episode inside the main import loop: when a
# fetch hangs or fails, the bot retries RIGHT AWAY (escalating timeout)
# before moving on to the next episode.  The strict end-of-loop retry pass
# then covers anything still missing.
IMMEDIATE_RETRIES = 3
COOKIES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cookies.txt")

SSSTIK_URL = "https://ssstik.io"

def _clean_url(url: str) -> str:
    """Strip tracking query params from a TikTok URL."""
    url = url.split("?")[0]
    return url.rstrip("/")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY") or ""
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "tiktok-downloader-download-tiktok-videos-no-watermark.p.rapidapi.com")

# ── Series extraction (pure HTTP — no browser) ──────────────────────────────

def _extract_episodes(url: str, progress_cb=None) -> list[dict]:
    """Extract a TikTok series' full episode list over plain HTTP.

    Engine: scripts/tiktok_series.py — httpx for the page's official
    ``dramaInfo`` metadata (real series name, poster, expected episode
    count) + yt-dlp's TikTok user extractor for the account's video list
    (paginated internal API, ~15 MB RAM, no browser).

    Returns [] on failure (degrade to single-video import).  The first
    entry may carry ``{"_meta": {"series_title": ..., "series_cover": ...,
    "last_ep_num": ...}}``.  ``progress_cb`` receives human-readable
    progress strings from the extraction thread.
    """
    try:
        from tiktok_series import extract_series
        return extract_series(url, progress_cb)
    except Exception as e:
        log.warning("Series extraction failed for %s: %s", url, clean_error(e))
        return []

# ── ANSI / cleanup helpers ──────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _supabase_dns_down() -> bool:
    """True when the Supabase project host no longer resolves.

    A paused (or deleted) Supabase project loses its DNS record, so EVERY
    request fails with `[Errno -2] Name or service not known` — on Render
    too. Detecting it lets us show an actionable message instead of raw
    errno noise.
    """
    try:
        from urllib.parse import urlparse
        host = urlparse(SUPABASE_URL).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    try:
        socket.gethostbyname(host)
        return False
    except OSError:
        return True


def clean_error(e: Exception) -> str:
    msg = strip_ansi(str(e))
    # A DNS-class error while the Supabase host itself is unresolvable
    # means the project is paused/deleted — no retry can fix that.
    try:
        if _is_dns_error(e) and _supabase_dns_down():
            return ("⚠️ Supabase төсөл PAUSED болсон байна "
                    "(DNS record олдсонгүй).\n"
                    "Шийдэх: supabase.com/dashboard нээж төслөө сонгоод "
                    "'Restore project' дарж сэргээнэ үү.\n"
                    "Сэргэсний дараа киноны линкийг дахин илгээнэ үү.")
    except Exception:
        pass
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return msg


async def _safe_edit(msg, text: str, **kwargs) -> None:
    """Update a Telegram status message without crashing the handler.

    Timeouts / network errors during long downloads are logged and
    ignored so the import keeps running in the background.

    When *msg* is None (console mode — e.g. clean_and_redownload.py)
    the text is printed to stdout instead.
    """
    if msg is None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print(text)
        return
    try:
        await msg.edit_text(text, **kwargs)
    except Exception as e:
        log.warning("Telegram status update skipped (%s): %s", type(e).__name__, clean_error(e)[:120])


# ── URL helpers ─────────────────────────────────────────────────────────────

_SHORT_LINK_RE = re.compile(r"https?://(?:vt|vm)\.tiktok\.com/\S+")
_TIKTOK_DOMAIN_RE = re.compile(r"tiktok\.com/@([\w.-]+)")
_AT_USER_RE = re.compile(r"^@([\w.-]+)$")


def is_short_link(text: str) -> bool:
    return bool(_SHORT_LINK_RE.search(text))


def resolve_short_url(url: str) -> str | None:
    try:
        resp = httpx.head(url, follow_redirects=True, timeout=15.0)
        return str(resp.url)
    except Exception:
        pass
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=15.0)
        return str(resp.url)
    except Exception as e:
        log.warning("Failed to resolve short link %s: %s", url, e)
        return None


def extract_username(text: str) -> str | None:
    m = _TIKTOK_DOMAIN_RE.search(text)
    if m:
        return m.group(1)
    m = _AT_USER_RE.match(text)
    if m:
        return m.group(1)
    return None


def is_profile(text: str) -> bool:
    """True when the message points at a PROFILE (or series/playlist page)
    rather than a single video or a short-drama deep link.

    Anything on tiktok.com/@user… that is NOT a /video/ URL is treated as a
    profile-like page — that includes bare @username links, '/short-dramas'
    tabs, '/series/<slug>' and '/playlist/<id>' pages.
    ``/shortdrama/episode/<dramaID>/<n>`` deep links are NOT profiles —
    they resolve to a full series via the drama APIs.
    """
    t = text.strip()
    if _AT_USER_RE.match(t):
        return True
    m = _TIKTOK_DOMAIN_RE.search(t)
    if m:
        remainder = t[m.end():]
        if "/video/" in remainder or "/shortdrama/" in remainder:
            return False
        return True
    return False


# ── Caption / title cleaning ──────────────────────────────────────────────

_HASHTAG_RE = re.compile(r"#(?=\w*[A-Za-z])\w+")  # hashtags containing at least one letter (preserves #4)
_EXT_RE = re.compile(r"\.\w{2,4}$")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE00-\uFE0F]")
_URL_RE = re.compile(r"https?://\S+")


def clean_caption(text: str) -> str:
    """Strip hashtags, emoji, URLs, file extensions and trim whitespace."""
    text = _HASHTAG_RE.sub("", text)
    text = _EXT_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = " ".join(text.split())
    return text.strip().rstrip(".,:; ")


def is_garbage_title(title: str) -> bool:
    """Return True if *title* is a bare filename, "Untitled", or otherwise unusable."""
    t = title.strip().lower()
    t = _EXT_RE.sub("", t)  # strip .mp4 / .jpg etc.
    if not t or t in ("untitled", "untitled video", "video", ""):
        return True
    # Looks like a bare filename: "EP001", "video_123", etc.
    if re.match(r"^[\w-]{2,30}$", t) and not re.search(r"[aeiou]{2,}", t):
        return True
    # Pure episode label with no real series name: "episode 1", "ep 2", "episode 1 ep"
    if re.match(r"^(?:ep(?:isode)?\.?\s*\d+)(?:\s+ep)?$", t) and len(t) < 30:
        return True
    return False


# ── Episode-title parsing ───────────────────────────────────────────────────

def parse_episode(title: str) -> tuple[str, int | None]:
    # Parse from the RAW title — clean_caption would strip "EP.10" → "EP"
    # (".10" looks like a file extension), destroying the episode number.
    pre = re.sub(r"\s*[—\-–]\s*(♬.*|original sound.*|sonido original.*)$", "", title).strip()
    # Strip stray trailing episode labels ("Episode 1 EP" → "Episode 1")
    pre = re.sub(r"\s+EP\.?\s*$", "", pre, flags=re.IGNORECASE)
    pre = re.sub(r"\s+Episode\s*$", "", pre, flags=re.IGNORECASE)
    patterns = [
        (re.compile(r"^(.+?)\s*EP\.?\s*(\d+)", re.IGNORECASE), 1, 2),
        (re.compile(r"^(.+?)\s+[—\-–]\s*(?:Ep|Episode)\s*(\d+)", re.IGNORECASE), 1, 2),
        (re.compile(r"^(.+?)\s+[|]\s*(?:Ep\.?|Episode)\s*(\d+)", re.IGNORECASE), 1, 2),
        (re.compile(r"^(.+?)\s+Eps?\s*(\d+)", re.IGNORECASE), 1, 2),
        (re.compile(r"^(.+?)\s+#\s*(\d+)"), 1, 2),
        (re.compile(r"^(.+?)\s+Part\s*(\d+)", re.IGNORECASE), 1, 2),
        (re.compile(r"^(.{5,}?)\s+(?:Ep\.?|Episode)\s*(\d+)", re.IGNORECASE), 1, 2),
    ]
    for pat, g_series, g_num in patterns:
        m = pat.search(pre)
        if m:
            series = clean_caption(m.group(g_series)).strip().rstrip(".,:; ")
            # Drop any leftover episode markers from the series part
            series = re.sub(r"\s+(?:EP|Episode|Ep)\.?\s*$", "", series, flags=re.IGNORECASE).strip()
            return series, int(m.group(g_num))
    return clean_caption(pre), None


# ── Real episode-number parsing (title / description / video text) ─────────

_EP_NUM_PATTERNS = [
    # "Episode 1", "EP1", "Ep. 5", "Part 3", "Chapter 2", "EP: 7", "EP #9"
    re.compile(r"\b(?:EP|Ep|Episode|Episodes|Part|Chapter|Ep)\s*[.:#\-–]?\s*(\d{1,4})\b", re.IGNORECASE),
    # "#12" / "12" / "3/30" — bare numbers (last resort)
    re.compile(r"^#?\s*(\d{1,4})\s*(?:/\s*\d{1,4})?\s*$"),
    # Chinese: 第5集
    re.compile(r"第\s*(\d{1,4})\s*[集话话]"),
]


def parse_episode_number(*texts) -> int | None:
    """Find the REAL episode number (1..5000) inside any of *texts*.

    Scans the video description/title text for "Episode 1", "EP1",
    "Ep. 5", "Part 3", "#12", "3/30" or "第5集" markers.  Keyword forms
    are preferred over bare numbers.  Returns None when nothing usable.
    """
    for text in texts:
        if not text:
            continue
        for pat in _EP_NUM_PATTERNS:
            m = pat.search(str(text))
            if m:
                n = int(m.group(1))
                if 0 < n <= 5000:
                    return n
    return None


def slug_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "series"


# ── Supabase Storage ───────────────────────────────────────────────────────

STORAGE_BUCKET = "videos"

# Transient network errors (DNS lookup failure `[Errno -2]`, connection
# reset, `getaddrinfo failed`) are retried with backoff — one bad DNS blip
# must never abort a bucket check or a video upload.
NET_RETRIES = 3
NET_BACKOFF = [2.0, 4.0, 8.0]


def _is_dns_error(e: Exception) -> bool:
    """True for DNS/connection-class errors worth an immediate retry."""
    msg = str(e).lower()
    return (
        "name or service not known" in msg
        or "getaddrinfo" in msg
        or "temporary failure in name resolution" in msg
        or "connection reset" in msg
        or "connection aborted" in msg
        or "connection refused" in msg
        or "network is unreachable" in msg
        or "nodename nor servname" in msg
        or isinstance(e, (ConnectionError, TimeoutError))
    )


def _net_retry(fn, *args, label: str = "", **kwargs):
    """Run *fn* retrying DNS/connection-class errors with backoff.
    Returns fn's result; raises the LAST exception when all tries fail
    (callers' try/except handles it — the app never crashes unguarded)."""
    last = None
    for i in range(NET_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if not _is_dns_error(e) or i == NET_RETRIES - 1:
                break
            log.warning("Transient network error%s (try %d/%d): %s — retrying in %.0fs",
                        f" [{label}]" if label else "", i + 1, NET_RETRIES, str(e)[:100],
                        NET_BACKOFF[i])
            import time as _t
            _t.sleep(NET_BACKOFF[i])
    raise last


def _ensure_videos_bucket(db: Client) -> bool:
    """Create the `videos` storage bucket if it doesn't exist.  Return True if ready."""
    import httpx as _httpx
    try:
        buckets = _net_retry(db.storage.list_buckets, label="bucket-list")
        if any(b.name == STORAGE_BUCKET for b in buckets):
            return True
    except Exception as e:
        log.warning("Storage bucket list failed (DNS/network): %s", str(e)[:120])
    # Create via REST (the supabase-py storage client API varies)
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }
        r = _net_retry(
            _httpx.post,
            f"{SUPABASE_URL}/storage/v1/bucket",
            json={"name": STORAGE_BUCKET, "public": True},
            headers=headers,
            timeout=10.0,
            label="bucket-create",
        )
        r.raise_for_status()
        log.info("Created public storage bucket '%s'", STORAGE_BUCKET)
        return True
    except Exception as e:
        log.warning("Storage bucket setup failed: %s", e)
        return False


def _upload_media(series_id: str, ep_num: int, src_url: str, prefix: str, content_type: str, preloaded_bytes: bytes | None = None) -> str:
    """Download *src_url* and upload it to ``{STORAGE_BUCKET}/{series_id}/{prefix}_{ep_num}``.

    If *preloaded_bytes* is provided it is used instead of downloading *src_url*.
    Video content smaller than MIN_VIDEO_SIZE is rejected (never uploaded).
    Returns the permanent public URL, or ``""`` on failure (callers retry).
    """
    import httpx as _httpx

    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.tiktok.com/",
        "Accept": "video/mp4,*/*",
    }

    content = preloaded_bytes
    if content is None:
        try:
            resp = _httpx.get(src_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=90.0)
            resp.raise_for_status()
            content = resp.content
        except Exception as e:
            log.warning("Failed to download %s for %s/%s-%s: %s", prefix, series_id, ep_num, src_url[-40:], e)
            return ""

    # Reject broken/empty video downloads — never upload a 0-byte file.
    if "video" in content_type and len(content) < MIN_VIDEO_SIZE:
        log.warning(
            "Refusing to upload %s/%s-%s: content is only %d bytes (< %d)",
            series_id, prefix, ep_num, len(content), MIN_VIDEO_SIZE,
        )
        return ""

    ext = "mp4" if "video" in content_type else "jpg"
    remote_path = f"{series_id}/{prefix}_{ep_num}.{ext}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "x-upsert": "true",
    }
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{remote_path}"

    try:
        r = _httpx.post(
            upload_url,
            content=content,
            headers={**headers, "Content-Type": content_type},
            timeout=120.0,
        )
        r.raise_for_status()
    except _httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        log.warning("Storage upload failed for %s (HTTP %s): %s | body: %s",
                     remote_path, e.response.status_code, e, body)
        return ""
    except Exception as e:
        log.warning("Storage upload failed for %s: %s", remote_path, e)
        return ""

    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{remote_path}"


# ── TikTok extraction ───────────────────────────────────────────────────────

# Optional comma-separated ssstik mirror hosts (env SSSTIK_MIRRORS, e.g.
# "ssstik.io,ssstik.info").  On a DNS/[Errno -2] / connection failure the
# downloader automatically rotates to the next mirror before giving up.
SSSTIK_MIRRORS = [
    h.strip() if "://" in h else f"https://{h.strip()}"
    for h in os.getenv("SSSTIK_MIRRORS", "").split(",") if h.strip()
]


def _ssstik_hosts() -> list[str]:
    hosts = [SSSTIK_URL]
    for base in SSSTIK_MIRRORS:
        if base not in hosts:
            hosts.append(base)
    return hosts


def _fetch_video_ssstik(url: str, custom_title: str | None = None) -> dict | None:
    """Extract video via ssstik.io free downloader.

    Scrapes the public ssstik.io service to get a no-watermark MP4 URL
    plus author and description metadata.  Returns a dict with the same
    shape as _fetch_video_ytdlp(), or None.

    If *custom_title* is provided, it overrides the extracted title.
    """
    last_err: Exception | None = None
    for base in _ssstik_hosts():
        try:
            s = httpx.Client(follow_redirects=True, timeout=30.0)
            s.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })

            # Step 1 — fetch homepage to get CSRF token
            r1 = _net_retry(s.get, base, label=f"ssstik-home-{base}")
            m = re.search(r"s_tt = '([^']+)'", r1.text)
            tt = m.group(1) if m else ""

            # Step 2 — submit video URL
            r2 = _net_retry(
                s.post, f"{base}/abc?url=dl",
                data={"id": url, "locale": "en", "tt": tt},
                label=f"ssstik-submit-{base}",
            )
            body = r2.text

            if "panel critical" in body or "serious problem" in body:
                log.warning("ssstik.io error for %s (TikTok unavailable or blocked)", url)
                return None

            # Extract no-watermark SD video URL
            video_match = re.search(
                r'href="([^"]+)"[^>]*class="[^"]*without_watermark[^"]*"', body
            )
            if not video_match:
                log.warning("ssstik.io: no without_watermark link in response for %s", url)
                return None
            video_url = video_match.group(1)

            # Author
            author_match = re.search(r"<h2>([^<]+)</h2>", body)
            username = (author_match.group(1) if author_match else "").strip()

            # Description
            desc_match = re.search(r'<p class="maintext">([^<]*)</p>', body, re.DOTALL)
            desc = desc_match.group(1).strip()[:500] if desc_match else "Untitled"

            # Use custom_title if provided, otherwise clean caption or @author fallback
            if custom_title:
                clean_title = custom_title
            else:
                clean_title = clean_caption(desc) or username
                if is_garbage_title(clean_title) and username:
                    clean_title = f"@{username}"

            # Thumbnail (avatar)
            avatar_match = re.search(
                r'<img[^>]*class="result_author"[^>]*src="([^"]+)"', body
            )
            avatar = avatar_match.group(1) if avatar_match else ""

            return {
                "webpage_url": url,
                "title": clean_title,
                "description": desc,
                "video_url": video_url,
                "thumbnail": avatar,
                "duration": 1,
                "username": username,
            }
        except Exception as e:
            last_err = e
            if _is_dns_error(e):
                log.warning("ssstik mirror %s failed (DNS/conn) — switching mirror: %s",
                            base, str(e)[:100])
                continue
            log.warning("ssstik.io request failed for %s: %s", url, clean_error(e))
            return None
    log.warning("ssstik all mirrors failed for %s: %s", url, str(last_err)[:120])
    return None


def _ydl_opts(**kw) -> dict:
    base = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    }
    if os.path.isfile(COOKIES_PATH):
        base["cookiefile"] = COOKIES_PATH
    base.update(kw)
    return base


def _fetch_video_ytdlp(url: str, custom_title: str | None = None) -> dict | None:
    """Fallback: extract video via yt-dlp.

    When the cookies.txt file exists, actually downloads the video data
    via yt-dlp (which handles TikTok CDN tokens properly) and returns
    the bytes in ``_video_bytes``.

    If *custom_title* is provided it overrides the extracted title.
    """
    import yt_dlp

    try:
        opts = _ydl_opts(format="best", noplaylist=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("yt-dlp failed for %s: %s", url, clean_error(e))
        return None

    uploader = info.get("uploader", "").strip()
    if custom_title:
        title = custom_title
    else:
        title = (info.get("title") or "").strip()
        title = re.sub(r"\s*[—\-–]\s*(♬.*|original sound.*|sonido original.*)$", "", title).strip()
        if not title or is_garbage_title(title):
            title = f"@{uploader}" if uploader else "Untitled"

    # Try to download the actual video bytes when cookies are available
    video_bytes = None
    video_content_type = "video/mp4"
    if os.path.isfile(COOKIES_PATH):
        try:
            tmp_dir = tempfile.mkdtemp()
            tmp_path = os.path.join(tmp_dir, "video.mp4")
            dl_opts = _ydl_opts(
                format="best",
                noplaylist=True,
                outtmpl=tmp_path,
            )
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([url])
            if os.path.isfile(tmp_path):
                with open(tmp_path, "rb") as f:
                    video_bytes = f.read()
                log.info("yt-dlp downloaded %d bytes for %s", len(video_bytes), url)
                try:
                    os.unlink(tmp_path)
                    os.rmdir(tmp_dir)
                except Exception:
                    pass
        except Exception as e:
            log.warning("yt-dlp download failed for %s: %s", url, clean_error(e))

    # Fallback: extract video URL from format data
    video_url = info.get("url") or ""
    if not video_url and info.get("formats"):
        best = max(info["formats"], key=lambda f: f.get("height", 0) or 0)
        video_url = best.get("url") or ""

    return {
        "webpage_url": info.get("webpage_url", url),
        "title": title,
        "description": (info.get("description") or title).strip()[:500],
        "video_url": video_url,
        "thumbnail": info.get("thumbnail") or "",
        "duration": max(1, round((info.get("duration") or 30) / 60)),
        "username": uploader,
        "_video_bytes": video_bytes,
        "_content_type": video_content_type,
    }


def _fetch_video_tikwm(url: str, custom_title: str | None = None) -> dict | None:
    """Extract video via tikwm.com free API (no API key needed)."""
    try:
        resp = _net_retry(
            httpx.post,
            "https://www.tikwm.com/api/",
            data={"url": url, "hd": 1},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30.0,
            label="tikwm",
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            log.warning("tikwm.com API error for %s: %s", url, body.get("msg", ""))
            return None
        data = body.get("data") or {}
        video_url = data.get("hdplay") or data.get("play") or ""
        if not video_url:
            log.warning("tikwm.com: no video URL in response for %s", url)
            return None

        title = data.get("title") or ""
        if custom_title:
            title = custom_title
        else:
            title = clean_caption(title) or data.get("author", "")
            if is_garbage_title(title) and data.get("author"):
                title = f"@{data['author']}"

        return {
            "webpage_url": url,
            "title": title,
            "description": (data.get("title") or title)[:500],
            "video_url": video_url,
            "thumbnail": data.get("cover") or "",
            "duration": max(1, round((data.get("duration") or 30) / 60)),
            "username": data.get("author") or "",
        }
    except Exception as e:
        log.warning("tikwm.com request failed for %s: %s", url, clean_error(e))
        return None


def _fetch_video(url: str, custom_title: str | None = None) -> dict | None:
    """Extract video from a TikTok URL.
    
    Download chain (pure HTTP — no browser):
      1. yt-dlp (with cookies) — downloads actual bytes, most reliable  
      2. ssstik.io — free downloader
      3. TikWM API — free API
      4. DIRECT: the video page's own playAddr mp4 URL (rehydration JSON) —
         no third-party processing, works even when every downloader is
         blocked (watermarked, but always available for public videos)
    
    If *custom_title* is provided it overrides the extracted title.
    Returns a dict with ``_video_bytes`` if the video was actually downloaded.
    """
    has_cookies = os.path.isfile(COOKIES_PATH)

    # 1. yt-dlp (with cookies) — gives bytes directly
    if has_cookies:
        result = _fetch_video_ytdlp(url, custom_title)
        if result and result.get("_video_bytes"):
            return result
        if result:
            log.info("yt-dlp: URL only (no bytes) for %s", url)

    # 2. ssstik.io
    result = _fetch_video_ssstik(url, custom_title)
    if result and result.get("video_url"):
        return result

    # 3. TikWM API
    result = _fetch_video_tikwm(url, custom_title)
    if result and result.get("video_url"):
        return result

    # 4. Direct public URL: TikTok's own playAddr mp4, no processing.
    result = _fetch_video_direct(url, custom_title)
    if result and result.get("video_url"):
        return result

    # 5. Retry yt-dlp for URL-only fallback (if not tried yet)
    if not has_cookies:
        result = _fetch_video_ytdlp(url, custom_title)
        if result:
            return result

    log.info("all downloaders failed for %s", url)
    return None


_DIRECT_RE = re.compile(
    r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.S,
)


def _fetch_video_direct(url: str, custom_title: str | None = None) -> dict | None:
    """DIRECT public-video fallback: read the video page's own rehydration
    JSON and return the playAddr mp4 CDN URL — zero third-party processing.

    Works for ANY public TikTok video even when ssstik/yt-dlp/TikWM are all
    blocked or stalled; the mp4 is watermarked but fully playable and the
    uploader accepts it (browser headers + Referer already in _upload_media).
    """
    try:
        import httpx as _httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        }
        cookie = _load_cookies_header()
        if cookie:
            headers["Cookie"] = cookie
        resp = _net_retry(
            _httpx.get, _clean_url(url),
            headers=headers, follow_redirects=True, timeout=15.0,
            label="direct-playaddr",
        )
        resp.raise_for_status()
        m = _DIRECT_RE.search(resp.text)
        if not m:
            return None
        data = json.loads(m.group(1))
        scope = (data or {}).get("__DEFAULT_SCOPE__") or {}
        item = (((scope.get("webapp.video-detail") or {}).get("itemInfo") or {})
                .get("itemStruct")) or {}
        play = (item.get("video") or {}).get("playAddr") or {}
        urls = play.get("urlList") or []
        if not urls or not str(urls[0]).startswith("http"):
            return None
        mp4 = str(urls[0])
        desc = str(item.get("desc") or "")
        title = custom_title or (desc.split("\n")[0][:100] if desc else "TikTok Video")
        log.info("Direct playAddr fallback OK: %s", mp4[-60:])
        return {
            "video_url": mp4,
            "title": title,
            "description": desc,
            "thumbnail": "",
            "duration": 1,
        }
    except Exception as e:
        log.warning("Direct playAddr fetch failed for %s: %s", url, str(e)[:100])
        return None


def _fetch_entries_ytdlp(username: str) -> list[dict] | None:
    """Fetch video IDs via yt-dlp (with cookies). Returns None on failure."""
    try:
        import yt_dlp

        opts = _ydl_opts(extract_flat="in_playlist", playlistend=50)
        if os.path.isfile(COOKIES_PATH):
            opts["cookiefile"] = COOKIES_PATH
            log.info("Using cookies from %s", COOKIES_PATH)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{username}", download=False)
        return info.get("entries") or []
    except Exception as e:
        log.warning("yt-dlp profile fetch failed for @%s: %s", username, clean_error(e))
        return None


def _load_cookies_header() -> str:
    """Read cookies.txt and return a Cookie header string."""
    if not os.path.isfile(COOKIES_PATH):
        return ""
    pairs = []
    with open(COOKIES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                pairs.append(f"{parts[5]}={parts[6]}")
    return "; ".join(pairs)


def _fetch_entries_web(username: str) -> list[dict] | None:
    """Scrape TikTok profile page HTML for video IDs.

    Extracts video IDs from the __UNIVERSAL_DATA_FOR_REHYDRATION__ script
    tag embedded in the profile page.  Works without a logged-in session
    as long as the cookies contain ttwid / msToken.
    """
    url = f"https://www.tiktok.com/@{username}"
    cookies_header = _load_cookies_header()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if cookies_header:
        headers["Cookie"] = cookies_header

    try:
        resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=30.0)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Web profile fetch failed for @%s: %s", username, clean_error(e))
        return None

    html = resp.text

    # Method 1: __UNIVERSAL_DATA_FOR_REHYDRATION__ (JSON)
    m = re.search(
        r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>'
        r'\s*(.*?)</script>',
        html,
        re.DOTALL,
    )
    data = None
    if m:
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    if data:
        # Navigate: defaultScope.userModule.users[username].posts
        users = (
            data.get("__DEFAULT_SCOPE__", {})
            .get("userModule", {})
            .get("users", {})
        )
        user_data = users.get(username) or {}
        posts = user_data.get("posts") or []
        entries = []
        for p in posts[:50]:
            if isinstance(p, dict):
                vid = p.get("id") or p.get("video_id") or p.get("id_str") or ""
                if vid:
                    entries.append({"id": vid})
            elif isinstance(p, str):
                entries.append({"id": p})
        if entries:
            return entries

    # Method 2: SIGI_STATE (legacy)
    m2 = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>\s*(.*?)</script>', html, re.DOTALL)
    if m2:
        try:
            data2 = json.loads(m2.group(1))
            props = data2.get("props", {}).get("pageProps", {})
            items = props.get("items") or props.get("videoList") or []
            entries = []
            for item in items[:50]:
                vid = item.get("id") or item.get("video_id") or ""
                if vid:
                    entries.append({"id": vid})
            if entries:
                return entries
        except json.JSONDecodeError:
            pass

    # Method 3: extract video IDs from inline JSON blobs in the HTML
    ids = re.findall(r'"id"\s*:\s*"(\d{17,})"', html)
    seen = set()
    entries = []
    for vid in ids:
        if vid not in seen:
            seen.add(vid)
            entries.append({"id": vid})
    if entries:
        log.info("Web profile: extracted %d video IDs from inline HTML for @%s", len(entries), username)
        return entries[:50]

    return None


def _fetch_entries_rapidapi(username: str) -> list[dict] | None:
    """Fetch video IDs via RapidAPI user feed endpoint. Returns None on failure."""
    if not RAPIDAPI_KEY:
        log.warning("RAPIDAPI_KEY not set — skipping RapidAPI fallback for @%s", username)
        return None
    try:
        resp = httpx.get(
            f"https://{RAPIDAPI_HOST}/user/posts",
            params={"unique_id": username, "count": 50},
            headers={
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": RAPIDAPI_HOST,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        log.warning("RapidAPI profile fetch failed for @%s: %s", username, clean_error(e))
        return None

    # Response format varies by provider — normalise to list of {id, url}
    raw = body.get("data") or body.get("videos") or body.get("items") or body.get("results") or []
    if not isinstance(raw, list):
        entries = raw.get("videos") or raw.get("items") or raw.get("list") or []
    else:
        entries = raw

    result = []
    for v in entries[:50]:
        if isinstance(v, dict):
            vid = v.get("id") or v.get("video_id") or ""
            if vid:
                result.append({"id": vid})
            elif v.get("url") or v.get("link") or v.get("video_url"):
                result.append({"url": v.get("url") or v.get("link") or v["video_url"]})
        elif isinstance(v, str):
            result.append({"id": v})
    return result


def _fetch_entries(username: str) -> list[dict]:
    """Fetch all video IDs from a TikTok profile.

    Tries: yt-dlp → web scraping → RapidAPI → empty.
    """
    entries = _fetch_entries_ytdlp(username)
    if entries:
        return entries

    log.info("yt-dlp failed, falling back to web scraping for @%s", username)
    entries = _fetch_entries_web(username)
    if entries:
        return entries

    log.info("web scrape failed, falling back to RapidAPI for @%s", username)
    entries = _fetch_entries_rapidapi(username)
    if entries:
        return entries

    log.error("All profile-scraping methods failed for @%s", username)
    return []


# ── Supabase ────────────────────────────────────────────────────────────────

class Store:
    def __init__(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in .env")
        # Hard timeouts on EVERY Supabase call (PostgREST + storage) — a
        # wedged DB/storage request can never hang the import loop again.
        from supabase import ClientOptions
        options = ClientOptions(
            postgrest_client_timeout=30,
            storage_client_timeout=30,
        )
        self.db: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
        self._storage_ready = _ensure_videos_bucket(self.db)
        self.has_status = self._probe_status_columns()

    def _probe_status_columns(self) -> bool:
        """True when the episodes table has the status/source_url columns
        (schema.sql section 2b).  Without them the bot runs in fallback
        mode: 'pending' = row with an empty video_url."""
        try:
            self.db.table("episodes").select("status, source_url").limit(1).execute()
            return True
        except Exception:
            log.warning(
                "Episodes table has no 'status'/'source_url' columns — running in "
                "fallback mode (pending = empty video_url). Run the ALTER "
                "statements from schema.sql (section 2b) to enable full tracking."
            )
            return False

    def upsert_series(self, sid: str, title: str, genre: str, desc: str, thumb: str) -> None:
        payload = {
            "id": sid, "title": title, "genre": genre,
            "description": desc or title,
            "play_count": 0, "episode_count": 0,
        }
        # Never overwrite an existing poster with an empty value — the
        # series keeps its official cover from a previous import.
        if thumb:
            payload["poster_url"] = thumb
            payload["banner_url"] = thumb
        self.db.table("series").upsert(payload, on_conflict="id").execute()

    def episode_exists(self, sid: str, num: int) -> bool:
        r = self.db.table("episodes").select("id", count="exact").eq("id", f"ep-{sid}-{num}").execute()
        return (r.count or 0) > 0

    def _storage_object_size(self, bucket: str, path: str) -> int | None:
        """Return the size in bytes of a storage object, or None if it is missing/error."""
        import httpx as _httpx
        prefix, _, name = path.rpartition("/")
        prefix = f"{prefix}/" if prefix else ""
        try:
            r = _httpx.post(
                f"{SUPABASE_URL}/storage/v1/object/list/{bucket}",
                json={"prefix": prefix, "limit": 1000, "offset": 0},
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )
            if r.status_code == 200:
                for obj in r.json():
                    if obj.get("name") == name:
                        return (obj.get("metadata") or {}).get("size")
        except Exception as e:
            log.warning("Storage size check failed %s/%s: %s", bucket, path, e)
        return None

    def is_video_url_broken(self, url: str) -> bool:
        """True when the video cannot play: missing/0-byte storage object or expired CDN link."""
        if not url:
            return True
        m = re.search(r"/storage/v1/object/public/([^/]+)/(.+)$", url)
        if m:
            size = self._storage_object_size(m.group(1), m.group(2))
            return size is None or size <= 0
        m = re.search(r"[?&]expire=(\d+)", url)
        if m:
            return int(m.group(1)) < time.time()
        try:
            import httpx as _httpx
            r = _httpx.head(url, follow_redirects=True, timeout=8.0)
            if r.status_code >= 400:
                return True
            ctype = (r.headers.get("content-type") or "").lower()
            if "video" in ctype or "octet-stream" in ctype:
                return False
            # CDN caches often serve HEAD as 200 even after the link expired —
            # confirm with a tiny ranged GET.
            g = _httpx.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True, timeout=8.0)
            return g.status_code >= 400
        except Exception:
            return True

    def get_episode_video_url(self, sid: str, num: int) -> str:
        try:
            r = self.db.table("episodes").select("video_url").eq("id", f"ep-{sid}-{num}").execute()
            row = (r.data or [{}])[0]
            return str(row.get("video_url") or "")
        except Exception:
            return ""

    def get_episode_thumbnail(self, sid: str, num: int) -> str:
        """Poster of an existing episode — used as the series poster when no
        official series cover and no EP 1 cover exist in the current batch."""
        try:
            r = self.db.table("episodes").select("thumbnail_url").eq("id", f"ep-{sid}-{num}").execute()
            row = (r.data or [{}])[0]
            return str(row.get("thumbnail_url") or "")
        except Exception:
            return ""

    def is_episode_video_broken(self, sid: str, num: int) -> bool:
        """True when the existing episode's video is 0-byte, expired or unreadable."""
        return self.is_video_url_broken(self.get_episode_video_url(sid, num))

    def is_episode_pending(self, sid: str, num: int) -> bool:
        """True when the episode row exists in the DB but is not playable yet
        (status='pending' / empty video_url).  Pending episodes are NEVER
        skipped as duplicates — the import always re-downloads and completes
        them."""
        cols = "status, video_url" if self.has_status else "video_url"
        try:
            r = self.db.table("episodes").select(cols).eq("id", f"ep-{sid}-{num}").execute()
            if not (r.data or []):
                return False
            row = r.data[0]
            if self.has_status:
                return str(row.get("status") or "") == "pending"
            return not (row.get("video_url") or "")
        except Exception as e:
            log.warning("Pending check failed for %s/%s: %s", sid, num, e)
            return False

    def get_series_episodes(self, sid: str) -> list[dict]:
        """Return every episode row of a series (used by the auto cleanup queue)."""
        try:
            r = self.db.table("episodes").select("*").eq("series_id", sid).execute()
            return list(r.data or [])
        except Exception as e:
            log.warning("Fetching episodes of %s failed: %s", sid, e)
            return []

    def upsert_episode_pending(self, sid: str, num: int, data: dict) -> None:
        """Write (or keep) a PENDING row: the episode exists in the DB but
        has no playable video yet.  The TikTok source URL is persisted so the
        auto cleanup queue can re-download it on any later import."""
        source = data.get("_page_url") or data.get("video_url") or ""
        row = {
            "id": f"ep-{sid}-{num}",
            "series_id": sid,
            "episode_number": num,
            "title": data.get("title") or f"EP.{num}",
            "description": source if not self.has_status else (data.get("description") or ""),
            "video_url": "",
            "thumbnail_url": "",
            "duration": 0,
            "is_free": num <= DEFAULT_FREE_FIRST,
        }
        if self.has_status:
            row["status"] = "pending"
            row["source_url"] = source
        try:
            self.db.table("episodes").upsert(row, on_conflict="id").execute()
            cnt = self.db.table("episodes").select("id", count="exact").eq("series_id", sid).execute().count or 0
            self.db.table("series").update({"episode_count": cnt}).eq("id", sid).execute()
        except Exception as e:
            log.error("Marking EP %s of %s as pending failed: %s", num, sid, e)

    def bulk_upsert_pending(self, sid: str, rows: list[dict]) -> None:
        """Write many PENDING episode rows in ONE bulk upsert call, so the
        whole series appears in the DB instantly (the upload loop then fills
        in the real video_url row by row)."""
        if not rows:
            return
        try:
            self.db.table("episodes").upsert(rows, on_conflict="id").execute()
            cnt = self.db.table("episodes").select("id", count="exact").eq("series_id", sid).execute().count or 0
            self.db.table("series").update({"episode_count": cnt}).eq("id", sid).execute()
        except Exception as e:
            log.error("Bulk pending upsert for %s failed (%d rows): %s", sid, len(rows), e)
            for row in rows:
                self.db.table("episodes").upsert(row, on_conflict="id").execute()

    def upload_video(self, sid: str, num: int, src_url: str, preloaded_bytes: bytes | None = None) -> str:
        if not self._storage_ready:
            return src_url
        return _upload_media(sid, num, src_url, "video", "video/mp4", preloaded_bytes)

    def upload_thumbnail(self, sid: str, num: int, src_url: str) -> str:
        if not self._storage_ready or not src_url:
            return src_url
        return _upload_media(sid, num, src_url, "thumb", "image/jpeg")

    def upsert_episode(self, sid: str, num: int, data: dict, free: bool) -> None:
        row = {
            "id": f"ep-{sid}-{num}",
            "series_id": sid,
            "episode_number": num,
            "title": data["title"],
            "description": data["description"],
            "video_url": data["video_url"],
            # thumbnail_url is NOT NULL with no default: a failed thumb
            # download must NEVER become None or the whole upsert (and the
            # parallel batch around it) crashes with 23502.  Write an empty
            # string instead — a later heal pass overwrites it.
            "thumbnail_url": data.get("thumbnail") or "",
            "duration": data["duration"],
            "is_free": free,
        }
        if self.has_status:
            # A successfully uploaded episode is fully 'ok' — clears any
            # earlier pending state.  source_url keeps the TikTok page URL
            # for future re-downloads (auto cleanup queue).
            row["status"] = "ok"
            row["source_url"] = data.get("_page_url") or data.get("video_url") or ""
        self.db.table("episodes").upsert(row, on_conflict="id").execute()
        cnt = self.db.table("episodes").select("id", count="exact").eq("series_id", sid).execute().count or 0
        self.db.table("series").update({"episode_count": cnt}).eq("id", sid).execute()


# ── Sequential job queue ─────────────────────────────────────────────────────
# Every import request (series link, /series, /single, /force, profile) is
# pushed onto ONE asyncio.Queue and processed by a single worker task, one
# job at a time.  A 69-episode series import must finish BEFORE the next
# link is even started — this guarantees:
#   • no two imports fight over TikTok rate limits
#   • at most ONE HTTP import is ever alive → RAM usage stays bounded
#   • the user sees a deterministic queue position instead of chaos
# Hard watchdog: a wedged job (network hang, endless retry loop) is
# cancelled after JOB_ABSOLUTE_TIMEOUT so it can NEVER block the queue
# forever — the next queued import proceeds automatically.
JOB_QUEUE: asyncio.Queue = asyncio.Queue()
_job_worker_task: asyncio.Task | None = None
JOB_ABSOLUTE_TIMEOUT = 2400  # 40 min per job — a stuck loop gets reset


async def _job_worker() -> None:
    """Lone consumer of JOB_QUEUE — runs jobs strictly sequentially.

    Queue items are (job, msg, label) so the user can be told when their
    job dies or times out instead of staring at an eternal spinner.
    """
    while True:
        job, msg, label = await JOB_QUEUE.get()
        try:
            await asyncio.wait_for(job(), timeout=JOB_ABSOLUTE_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("Job exceeded %ds — cancelled; next queued job proceeds",
                      JOB_ABSOLUTE_TIMEOUT)
            try:
                await msg.reply_text(
                    f"⌛ *{label}* {JOB_ABSOLUTE_TIMEOUT // 60} минутад дуусаагүй "
                    "тул цуцаллаа. Дахин оролдоно уу — үргэлжлэл нь автоматаар "
                    "нөхөгдөнө.",
                    parse_mode="Markdown",
                )
            except Exception:
                log.exception("Failed to deliver job-timeout notice")
        except Exception as e:
            log.exception("Job worker: job crashed")
            try:
                await msg.reply_text(
                    f"❌ *{label}* амжилтгүй боллоо: {clean_error(e)}",
                    parse_mode="Markdown",
                )
            except Exception:
                log.exception("Failed to deliver job-crash notice")
        finally:
            JOB_QUEUE.task_done()


def _ensure_job_worker() -> None:
    """Start the worker task on the running event loop (first use only)."""
    global _job_worker_task
    if _job_worker_task is None or _job_worker_task.done():
        _job_worker_task = asyncio.get_running_loop().create_task(_job_worker())


async def _enqueue_job(msg, label: str, job) -> None:
    """Push *job* onto the sequential queue and reply with its position."""
    _ensure_job_worker()
    pos = JOB_QUEUE.qsize() + 1
    if pos == 1:
        reply = f"⏳ *{label}* эхэлж байна…"
    else:
        reply = (
            f"⏳ *{label}* дараалалд орлоо (байр: #{pos}).\n"
            "Өмнөх импорт дууссаны дараа автоматаар эхэлнэ."
        )
    await msg.reply_text(reply, parse_mode="Markdown")
    await JOB_QUEUE.put((job, msg, label))


# ── Bot handlers ────────────────────────────────────────────────────────────

async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await upd.message.reply_text(
        "🤖 *TikTok → Supabase Bot*\n\n"
        "**Series Import** (auto-detect full series from a video URL)\n"
        "• Send any TikTok video URL from a drama/series\n"
        "• Bot extracts all episodes via HTTP & imports them\n"
        "• Requires: run `/login` once first\n\n"
        "**Manual Series Import**\n"
        "• `/series <url>` — extract episodes from a specific video\n\n"
        "**Force Re-import**\n"
        "• `/force <url>` — re-download & overwrite existing episodes\n"
        "• Broken/0-byte/expired videos are re-imported automatically on every import\n\n"
        "**Single Video** (no series scan)\n"
        "• `/single <url>` — import ONLY the given video, no playlist extraction\n"
        "• Episode number is read from the video's title/description\n\n"
        "**Profile Import**\n"
        "• `@username` or `tiktok.com/@user` — up to 50 videos\n\n"
        "**Single Video Import**\n"
        "• Short link (`vt.tiktok.com/…`) or full TikTok URL\n"
        "• Add custom text before/after the URL to set the title\n\n"
        "Titles like *Title EP.1* or *Title - Episode 2* are parsed into series + episode.\n"
        "Episodes without a number get sequential numbers.\n"
        "First 2 episodes are marked free automatically.",
        parse_mode="Markdown",
    )


async def cmd_login(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy command — browser login is gone; HTTP needs no session."""
    await upd.message.reply_text(
        "ℹ️ Browser login is no longer needed — the bot runs 100% via HTTP "
        "(httpx + yt-dlp).\n"
        "Cookies remain supported via `cookies.txt` (project root) or "
        "`TIKTOK_COOKIES_FILE`.",
        parse_mode="Markdown",
    )


async def cmd_version(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Remote diagnostics: which commit is this service actually running?"""
    await upd.message.reply_text(
        f"🧬 Bot commit: `{BOT_VERSION}`\n"
        f"🐍 Python {sys.version.split()[0]} • workers={IMPORT_WORKERS}",
        parse_mode="Markdown",
    )


async def cmd_series(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Extract all episodes from a TikTok series/playlist URL."""
    args = ctx.args
    if not args:
        await upd.message.reply_text("Usage: /series <tiktok-video-url>")
        return

    url = args[0]
    if "tiktok.com/@" not in url:
        url = "https://www.tiktok.com/@" + url.lstrip("@")
        if "/video/" not in url and not re.search(r"/\d+", url):
            await upd.message.reply_text("Please provide a full TikTok video URL.")
            return

    async def job() -> None:
        status = await upd.message.reply_text("🔍 Analysing video for series episodes…")
        await _playlist(url, status)

    await _enqueue_job(upd.message, "Цуврал импорт", job)


async def _handle_text(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    assert upd.message and upd.message.text
    raw_text = upd.message.text.strip()

    # Extract custom title from message text (text before/after the URL)
    custom_title = None
    url_match = re.search(r"https?://\S+", raw_text)
    if url_match:
        # Text before the URL
        before = raw_text[:url_match.start()].strip()
        if before and not re.match(r"^[@#]\S*$", before) and not before.startswith("/"):
            custom_title = before
        # Text after the URL (if before was empty)
        if not custom_title:
            after = raw_text[url_match.end():].strip()
            if after and not re.match(r"^[@#]\S*$", after):
                custom_title = after
        text = url_match.group(0)
    else:
        text = raw_text

    if is_short_link(text):
        status = await upd.message.reply_text("🔗 Resolving short link…")
        resolved = await asyncio.to_thread(resolve_short_url, text)
        if not resolved:
            await _safe_edit(status, "❌ Could not resolve that TikTok short link.")
            return
        text = resolved
        await _safe_edit(status, f"🔗 Resolved to `{resolved[:60]}…`")
    else:
        status = await upd.message.reply_text("⏳ Processing…")

    username = extract_username(text)
    if not username:
        await _safe_edit(status, "❌ No TikTok username or video link found in your message.")
        return

    # Store custom title in context for _single to use
    if custom_title:
        ctx.user_data["custom_title"] = clean_caption(custom_title) or custom_title
    else:
        ctx.user_data.pop("custom_title", None)

    try:
        if is_profile(text):
            # Profile import is pure HTTP already (yt-dlp + scraping chain).
            await _profile(username, status, force)
        else:
            # Single video: series extraction via pure HTTP first
            await _safe_edit(status, "🔍 Checking for series episodes…")
            loop = asyncio.get_running_loop()

            def progress_cb(text):
                asyncio.run_coroutine_threadsafe(_safe_edit(status, text), loop)

            episodes = await asyncio.wait_for(
                asyncio.to_thread(_extract_episodes, text, progress_cb),
                timeout=600,
            )
            if len(episodes) > 1:
                log.info("Found %d episodes via HTTP extraction, importing series", len(episodes))
                # Warn when extraction came back far short of the series'
                # real total (e.g. 6/60 after a network failure) — the
                # user must never see a silently partial import.
                try:
                    meta = episodes[0].get("_meta") or {}
                    last_ep = meta.get("last_ep_num")
                    found = len(episodes) - 1
                    if last_ep and found < last_ep - 2:
                        await _safe_edit(status, f"⚠️ Зөвхөн {found}/{last_ep} анги олдлоо — "
                                                 f"олдсоныг нь импортлож эхэлж байна.\n"
                                                 f"Дараа нь линкийг дахин илгээвэл үлдсэн ангиуд нөхөгдөнө.")
                except Exception:
                    pass
                await _playlist_from_episodes(episodes, status, force=force)
                return
            elif episodes:
                log.info("HTTP extraction: only current video detected (no series) — importing as single")
                await _safe_edit(status, "⚠️ No series playlist detected — importing this video only")
            else:
                log.info("HTTP extraction returned empty — falling through to single")
            await _single(text, status, ctx, force)
    except Exception as e:
        log.exception("Handler error")
        await _safe_edit(status, f"❌ Error: {clean_error(e)}")


async def cmd_single(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Import ONE video only — never scans/creates the series playlist."""
    if not upd.message or not upd.message.text or not re.search(r"https?://\S+", upd.message.text):
        await upd.message.reply_text(
            "Usage: /single <tiktok-video-url>\n\n"
            "Downloads ONLY the given video (the right-side Series Playlist "
            "is NOT scanned).\n"
            "The episode number is read from the video's title/description "
            "and the video is added/updated in DB + storage directly.",
        )
        return

    async def job() -> None:
        status = await upd.message.reply_text("🎬 Single video import (no series scan)…")
        m = re.search(r"https?://\S+", upd.message.text)
        url = m.group(0)
        # Custom title before/after the URL (same as plain messages)
        before = upd.message.text[:m.start()].strip()
        after = upd.message.text[m.end():].strip()
        custom = (before or after).strip()
        if custom and not re.match(r"^[@#]\S*$", custom) and not custom.startswith("/"):
            ctx.user_data["custom_title"] = clean_caption(custom) or custom
        else:
            ctx.user_data.pop("custom_title", None)
        await _single(url, status, ctx)

    await _enqueue_job(upd.message, "Ганц видео импорт", job)


async def handle_msg(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def job() -> None:
        await _handle_text(upd, ctx, force=False)

    await _enqueue_job(upd.message, "Импорт", job)


async def cmd_force(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-import a video/series and OVERWRITE existing episodes unconditionally."""
    if not upd.message or not upd.message.text or not re.search(r"https?://\S+", upd.message.text):
        await upd.message.reply_text(
            "Usage: /force <tiktok-video-url>\n\n"
            "Re-downloads the video/series and OVERWRITES existing episodes "
            "(re-uploads to Supabase Storage and updates the DB), "
            "even when the current video looks healthy.",
        )
        return

    async def job() -> None:
        await _handle_text(upd, ctx, force=True)

    await _enqueue_job(upd.message, "Давхар импорт (force)", job)


BATCH_LIMIT = 50

# Parallel source-extraction: episodes are fetched FETCH_CONCURRENCY at a
# time (the same batching the profile import already uses safely) — a
# 50-episode series no longer runs source extraction one-by-one.
FETCH_CONCURRENCY = 4


async def _db_call(fn, *args, **kwargs):
    """Run a blocking Supabase call OFF the event loop with a hard
    watchdog (postgrest_client_timeout covers the HTTP layer too, this
    is the outer safety net).  Returns None on timeout — the callers'
    own guards treat it as a failure and move on."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=DB_WATCHDOG,
        )
    except asyncio.TimeoutError:
        log.error("DB call timed out after %.0fs: %s",
                  DB_WATCHDOG, getattr(fn, "__name__", "store_call"))
        return None
    except Exception:
        raise


async def _upload_video_watchdog(store, skey: str, ep: int, src_url: str,
                                 preloaded_bytes: bytes | None = None) -> str:
    """Upload one episode's video with a hard watchdog — a stalled CDN
    download or storage POST (up to 2 min each inside) can never hang
    the import again.  Returns the public URL or "" (callers retry/skip)."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(store.upload_video, skey, ep, src_url, preloaded_bytes),
            timeout=UPLOAD_WATCHDOG,
        )
    except asyncio.TimeoutError:
        log.error("Upload watchdog: EP %s of %s stalled > %.0fs — moving on",
                  ep, skey, UPLOAD_WATCHDOG)
        return ""
    except Exception:
        raise


async def _fetch_episode_source(url: str, custom_title: str | None = None) -> dict | None:
    """Watchdog-guarded source fetch for one episode.

    If the fetch shows no progress within FETCH_WATCHDOG seconds, the
    episode is force-skipped and the import moves on to the next one —
    a stalled TikTok/ssstik/yt-dlp call can never hang the loop again.
    Safe to run in parallel via asyncio.gather (no video bytes kept)."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_video, url, custom_title),
            timeout=FETCH_WATCHDOG,
        )
    except asyncio.TimeoutError:
        log.warning("Watchdog: %s stalled > %.0fs — force-skipping to next episode",
                    url, FETCH_WATCHDOG)
        return None
    except Exception as e:
        log.error("Fetch crashed for %s: %s — skipping to next episode",
                  url, clean_error(e))
        return None


async def _profile(username: str, msg, force: bool = False) -> None:
    await _safe_edit(msg, f"⏳ Fetching video list from `@{username}`…", parse_mode="Markdown")
    entries = await asyncio.to_thread(_fetch_entries, username)
    if not entries:
        await _safe_edit(msg, f"❌ No videos found for `@{username}`.", parse_mode="Markdown")
        return

    urls = []
    for e in entries[:BATCH_LIMIT]:
        uid = e.get("id") or e.get("url", "")
        if not uid:
            continue
        urls.append(f"https://www.tiktok.com/@{username}/video/{uid}" if not uid.startswith("http") else uid)

    total = len(urls)
    videos = []
    for i in range(0, total, 5):
        batch = [asyncio.to_thread(_fetch_video, u) for u in urls[i:i+5]]
        results = await asyncio.gather(*batch)
        for r in results:
            if r and r.get("video_url"):
                # Stream, don't accumulate: raw bytes for 50 videos would
                # blow the container RAM — upload re-fetches the CDN URL.
                r.pop("_video_bytes", None)
                videos.append(r)
        await _safe_edit(
            msg,
            f"⏳ Processing episode {len(videos)}/{total}…\n"
            f"(extracting metadata from `@{username}`)",
            parse_mode="Markdown",
        )

    if not videos:
        await _safe_edit(msg, "❌ Could not extract any video details.")
        return
    await _insert(msg, videos, force)


async def _single(url: str, msg, ctx: ContextTypes.DEFAULT_TYPE | None = None, force: bool = False) -> None:
    custom_title = ctx.user_data.get("custom_title") if ctx else None
    await _safe_edit(msg, "⏳ Extracting video…")
    clean = _clean_url(url)
    # Immediate re-sync: up to IMMEDIATE_RETRIES attempts with an
    # escalating timeout, so a single network hang never fails the import.
    data = None
    for attempt in range(1, IMMEDIATE_RETRIES + 1):
        timeout = min(EPISODE_FETCH_TIMEOUT * (2 ** (attempt - 1)), MAX_RETRY_TIMEOUT)
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(_fetch_video, clean, custom_title),
                timeout=timeout,
            )
            if data and data.get("video_url"):
                break
        except asyncio.TimeoutError:
            log.warning("Video fetch attempt %d/%d timed out (%.0fs) for %s",
                        attempt, IMMEDIATE_RETRIES, timeout, url)
            await _safe_edit(msg, f"⏳ Attempt {attempt}/{IMMEDIATE_RETRIES} timed out — re-syncing…")
        if attempt < IMMEDIATE_RETRIES:
            await asyncio.sleep(2)
    if not data or not data.get("video_url"):
        log.error("All download methods failed for %s", url)
        await _safe_edit(msg, "❌ Could not extract video from that TikTok URL.")
        return
    parsed = parse_episode_number(data.get("description", ""), data.get("title", ""))
    data["_ep"] = parsed
    data["_page_url"] = clean
    await _insert(msg, [data], force)


async def _playlist(url: str, msg, force: bool = False) -> None:
    """Extract episodes from a series URL and import them (pure HTTP)."""
    await _safe_edit(msg, "⏳ Extracting episodes over HTTP…")
    loop = asyncio.get_running_loop()

    def progress_cb(text):
        asyncio.run_coroutine_threadsafe(_safe_edit(msg, text), loop)

    episodes = await asyncio.wait_for(
        asyncio.to_thread(_extract_episodes, url, progress_cb),
        timeout=300,
    )
    if not episodes:
        await _safe_edit(msg, "❌ No episodes found. Is this video part of a series?")
        return
    await _playlist_from_episodes(episodes, msg, force=force)


# ── Pending-first series import ─────────────────────────────────────────────
# The foreground flow only REGISTERS the full episode list (status=pending)
# in the DB and replies "N анги олдлоо" — the heavy download+upload runs in
# a detached background task, so a 50-episode import never blocks the job
# queue and the user sees the complete series instantly.
_BG_TASKS: dict[str, asyncio.Task] = {}


def _start_background_import(skey: str, task: asyncio.Task) -> None:
    """Keep at most ONE detached import per series: re-sending the same
    link while a background download already runs reuses it instead of
    doubling the work (and the TikTok rate-limit pressure)."""
    current = _BG_TASKS.get(skey)
    if current and not current.done():
        log.info("Background import for %s already running — reusing it", skey)
        return
    _BG_TASKS[skey] = task
    task.add_done_callback(
        lambda t: _BG_TASKS.pop(skey, None) if _BG_TASKS.get(skey) is t else None
    )


async def _playlist_from_episodes(episodes: list[dict], msg, series_title: str | None = None, force: bool = False) -> None:
    """FAST foreground flow: parse metadata, register EVERY episode of the
    series in the DB as PENDING (one bulk upsert — the app shows the full
    series + all episode numbers right away), tell the user "N анги олдлоо",
    then hand the heavy work to a detached background task.  Returns within
    seconds — the queue never waits on uploads again."""
    # Extract metadata from the first entry
    last_ep_num = None
    last_ep_url = None
    series_cover = None
    topup_username = ""
    topup_sec_uid = ""
    topup_drama_id = ""
    if episodes and episodes[0].get("_meta"):
        meta = episodes.pop(0)["_meta"]
        series_title = series_title or meta.get("series_title")
        last_ep_num = meta.get("last_ep_num")
        last_ep_url = meta.get("last_ep_url")
        series_cover = meta.get("series_cover") or None
        # Account identity — lets the top-up loop pull ALL episode IDs
        # from the bulk item_list API instead of re-extracting the series.
        topup_username = meta.get("username") or ""
        topup_sec_uid = meta.get("sec_uid") or ""
        topup_drama_id = meta.get("drama_id") or ""
    if not episodes:
        await _safe_edit(msg, "❌ No episodes extracted.")
        return
    # Never invent a series name — garbage titles fall back to @author,
    # or (last resort) the raw title; _insert() re-validates anyway.
    if not series_title or is_garbage_title(series_title):
        if topup_username:
            series_title = f"@{topup_username}"
        elif not series_title:
            series_title = "TikTok Series"

    total = len(episodes)
    skey = slug_id(series_title)
    store = Store()
    await _db_call(store.upsert_series, skey, series_title, DEFAULT_GENRE,
                    series_title, series_cover or "")

    # ── Phase 1 (instant): the FULL episode list as PENDING rows — all
    #    episode numbers appear in the DB before any download starts.
    #    Already-healthy rows are NEVER clobbered (re-sending a finished
    #    series must not reset its playable videos to pending).
    rows: list[dict] = []
    existing: dict[int, dict] = {}
    try:
        all_rows = await _db_call(store.get_series_episodes, skey) or []
        existing = {
            int(r.get("episode_number") or 0): r
            for r in all_rows
        }
    except Exception:
        pass
    for ep in episodes:
        try:
            n = int(ep.get("episode") or 0)
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue
        if not force and n in existing:
            row0 = existing[n]
            healthy = (str(row0.get("video_url") or "")
                       and not (store.has_status
                                and str(row0.get("status") or "") == "pending"))
            if healthy:
                continue  # already playable — never clobber
        src = _clean_url(ep["url"])
        row = {
            "id": f"ep-{skey}-{n}",
            "series_id": skey,
            "episode_number": n,
            "title": f"{series_title} EP.{n}",
            "description": src if not store.has_status else "",
            "video_url": "",
            "thumbnail_url": "",
            "duration": 0,
            "is_free": n <= DEFAULT_FREE_FIRST,
        }
        if store.has_status:
            row["status"] = "pending"
            row["source_url"] = src
        rows.append(row)
    if rows:
        await _db_call(store.bulk_upsert_pending, skey, rows)
    await _safe_edit(msg, f"✅ {total} анги олдлоо — татаж эхэлж байна…")
    log.info("Pending-first: %d episodes of %s registered instantly",
             len(rows), series_title)

    # ── Phase 2 (background): download + upload + verify + top-up.
    task = asyncio.get_running_loop().create_task(
        _background_series_import(msg, episodes, series_title, last_ep_num,
                                  last_ep_url, series_cover, force,
                                  topup_username, topup_sec_uid,
                                  topup_drama_id))
    _start_background_import(skey, task)


async def _complete_series_pending_rows(skey: str, stitle: str,
                                        rows: list[dict]) -> None:
    """Background completion of one series' pending rows (startup sweep).
    Each pending episode is fetched (watchdog), uploaded (watchdog) and
    upserted as healthy — never blocks the queue."""
    store = Store()
    done = 0
    for r in rows:
        try:
            ep = int(r.get("episode_number") or 0)
            src = str(r.get("source_url") or "") or str(r.get("description") or "")
            if not ep or not src.startswith("http"):
                continue
            data = await _fetch_episode_source(src, f"{stitle} EP.{ep}")
            if not data or not data.get("video_url"):
                continue
            rvideo = await _upload_video_watchdog(
                store, skey, ep, data["video_url"], data.get("_video_bytes") or None)
            data.pop("_video_bytes", None)
            if not rvideo:
                continue
            data["title"] = f"{stitle} EP.{ep}"
            data["_page_url"] = src
            data["thumbnail"] = data.get("thumbnail") or ""
            await _db_call(store.upsert_episode, skey, ep, data,
                           ep <= DEFAULT_FREE_FIRST)
            done += 1
        except Exception as e:
            log.warning("Startup sweep: EP %s of %s failed: %s",
                        r.get("episode_number"), stitle, clean_error(e))
    if done:
        log.info("Startup sweep: completed %d pending episodes of %s", done, stitle)


async def _startup_pending_sweep() -> None:
    """Self-healing sweep on bot start: every series with pending/incomplete
    episodes (crash mid-import, Render redeploy) is completed in the
    background — no import is ever left half-done."""
    try:
        await asyncio.sleep(15)  # let polling + first updates settle
        store = Store()
        if not store.has_status:
            log.info("Startup sweep: no status column — skipping")
            return
        try:
            resp = (store.db.table("episodes")
                    .select("series_id, episode_number, source_url, title, status")
                    .eq("status", "pending")
                    .limit(400)
                    .execute())
        except Exception as e:
            log.warning("Startup sweep query failed: %s", clean_error(e))
            return
        pending = resp.data if resp else []
        if not pending:
            log.info("Startup sweep: no pending episodes")
            return
        by_series: dict[str, list[dict]] = {}
        for r in pending:
            by_series.setdefault(str(r.get("series_id") or ""), []).append(r)
        for skey, srows in by_series.items():
            stitle = skey
            try:
                sresp = (store.db.table("series")
                         .select("id, title").eq("id", skey).execute())
                if sresp and sresp.data:
                    stitle = str(sresp.data[0].get("title") or skey)
            except Exception:
                pass
            log.info("Startup sweep: %d pending episodes of %s — completing",
                     len(srows), stitle)
            asyncio.get_running_loop().create_task(
                _complete_series_pending_rows(skey, stitle, srows))
    except Exception as e:
        log.warning("Startup sweep crashed: %s", clean_error(e))


async def _background_series_import(msg, episodes: list[dict], series_title: str,
                                    last_ep_num: int | None,
                                    last_ep_url: str | None,
                                    series_cover: str | None,
                                    force: bool = False,
                                    topup_username: str = "",
                                    topup_sec_uid: str = "",
                                    topup_drama_id: str = "") -> None:
    """Crash-proof wrapper around the heavy import: whatever happens to the
    pipeline (exception, watchdog skip, cancellation), a final completeness
    sweep ALWAYS runs so the series is never left with gaps."""
    try:
        await _background_series_import_impl(
            msg, episodes, series_title, last_ep_num, last_ep_url,
            series_cover, force, topup_username, topup_sec_uid,
            topup_drama_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Background series import crashed for %s — sweeping pending rows",
                      series_title)
    finally:
        if last_ep_num and series_title:
            try:
                await _final_sweep_for_series(series_title, last_ep_num)
            except Exception:
                log.exception("Final sweep crashed for %s", series_title)


async def _final_sweep_for_series(series_title: str, last_ep_num: int) -> None:
    """100%-completeness self-check: after the import (and even after a
    crash) every episode 1..N must be healthy in the DB.  Anything still
    missing is re-imported from its stored source_url (or the bulk API)
    until complete or the hard budget expires."""
    store = Store()
    skey = slug_id(series_title)
    have = await _healthy_db_episodes(store, skey, last_ep_num)
    missing = sorted({n for n in range(1, last_ep_num + 1)} - have)
    if not missing:
        log.info("Final sweep for %s: already complete (%d/%d)",
                 series_title, last_ep_num, last_ep_num)
        return
    log.info("Final sweep for %s: %d episodes still missing — re-importing",
             series_title, len(missing))

    # Source map: stored source_url (pending rows) — the source of truth
    # for every episode this run registered.
    src_by_ep: dict[int, str] = {}
    try:
        rows = await _db_call(store.get_series_episodes, skey) or []
        for r in rows:
            ep = int(r.get("episode_number") or 0)
            if ep not in missing:
                continue
            src = str(r.get("source_url") or "") or str(r.get("description") or "")
            if src and src.startswith("http"):
                src_by_ep[ep] = src
    except Exception as e:
        log.warning("Final sweep source map failed: %s", clean_error(e))

    sweep_deadline = time.monotonic() + 20 * 60
    while missing and time.monotonic() < sweep_deadline:
        still_missing: list[int] = []
        for ep in missing:
            src = src_by_ep.get(ep)
            if not src:
                continue
            data = await _fetch_episode_source(src, f"{series_title} EP.{ep}")
            if not data or not data.get("video_url"):
                still_missing.append(ep)
                continue
            rvideo = await _upload_video_watchdog(
                store, skey, ep, data["video_url"], data.get("_video_bytes") or None)
            data.pop("_video_bytes", None)
            if not rvideo:
                still_missing.append(ep)
                continue
            data["title"] = f"{series_title} EP.{ep}"
            data["_page_url"] = src
            data["thumbnail"] = data.get("thumbnail") or ""
            await _db_call(store.upsert_episode, skey, ep, data, ep <= DEFAULT_FREE_FIRST)
            log.info("Final sweep: EP %s of %s completed", ep, series_title)
        if still_missing == missing:
            break  # no progress this round — stop burning requests
        missing = still_missing
        if missing:
            await asyncio.sleep(15)

    have = await _healthy_db_episodes(store, skey, last_ep_num)
    if len(have) == last_ep_num:
        log.info("Final sweep for %s: COMPLETE %d/%d", series_title,
                 last_ep_num, last_ep_num)
    else:
        log.warning("Final sweep for %s: still %d/%d (budget/rate-limited)",
                    series_title, len(have), last_ep_num)


async def _background_series_import_impl(msg, episodes: list[dict], series_title: str,
                                         last_ep_num: int | None,
                                         last_ep_url: str | None,
                                         series_cover: str | None,
                                         force: bool = False,
                                         topup_username: str = "",
                                         topup_sec_uid: str = "",
                                         topup_drama_id: str = "") -> None:
    """Detached heavy import: extract every episode's video source
    (watchdog-guarded), upload to Supabase, verify, and top up anything
    still missing — runs in the background and never blocks the queue."""
    total = len(episodes)
    await _safe_edit(msg, f"📦 Found {total} episodes in '{series_title or 'unknown series'}'. Extracting video sources…")

    videos = []
    failed: list[dict] = []
    for batch_start in range(0, total, FETCH_CONCURRENCY):
        batch = episodes[batch_start:batch_start + FETCH_CONCURRENCY]
        end = min(batch_start + FETCH_CONCURRENCY, total)
        await _safe_edit(
            msg,
            f"⏳ Extracting episodes {batch_start + 1}–{end}/{total}…",
        )
        # Immediate re-sync with escalating per-attempt timeout; runs
        # FETCH_CONCURRENCY episodes in parallel so a 50-episode series
        # no longer extracts one-by-one.
        results = await asyncio.gather(
            *[_fetch_episode_source(_clean_url(ep["url"])) for ep in batch]
        )
        for ep, data in zip(batch, results):
            if data and data.get("video_url"):
                # Real episode number wins — parsed from the video's
                # description/title text ("Episode 1", "EP1", "Part 3", …).
                # Falls back to the sidebar position from extraction.
                parsed = parse_episode_number(data.get("description", ""), data.get("title", ""))
                data["_ep"] = parsed if parsed is not None else ep["episode"]
                if series_title:
                    data["title"] = f"{series_title} EP.{data['_ep']}"
                elif not data.get("title"):
                    data["title"] = f"EP.{data['_ep']}"
                data["_page_url"] = _clean_url(ep["url"])
                # STREAMING, NOT ACCUMULATING: do not keep this episode's raw
                # video bytes in the list — 40–50 episodes × 30–60 MB would OOM
                # the container before _insert even runs.  The upload step
                # re-fetches the CDN URL once at insert time (quick re-download,
                # bounded RAM).
                data.pop("_video_bytes", None)
                videos.append(data)
                log.info("Extractor OK for EP %s: %s", ep['episode'], ep["url"])
            else:
                log.error("All download methods FAILED for EP %s: %s", ep['episode'], ep["url"])
                failed.append(ep)

    # ── Strict retry pass: every failed episode is re-fetched up to
    #    MAX_FETCH_ATTEMPTS times.  Each attempt runs the FULL fallback
    #    chain (yt-dlp → ssstik → TikWM).  Episodes still
    #    failing after that are queued for _insert's own retry pass —
    #    a series is never reported complete while any episode is missing.
    if failed:
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            if not failed:
                break
            timeout = min(EPISODE_FETCH_TIMEOUT * (2 ** (attempt - 1)), MAX_RETRY_TIMEOUT)
            failed_nums = ", ".join(f"EP {e['episode']}" for e in failed[:6])
            if len(failed) > 6:
                failed_nums += "…"
            await _safe_edit(
                msg,
                f"🔄 Retrying {len(failed)} episode(s): {failed_nums} "
                f"(attempt {attempt}/{MAX_FETCH_ATTEMPTS}, timeout {timeout:.0f}s)…",
            )
            still_failed: list[dict] = []
            for ep in failed:
                ep_url = _clean_url(ep["url"])
                try:
                    data = await asyncio.wait_for(
                        asyncio.to_thread(_fetch_video, ep_url),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    still_failed.append(ep)
                    continue
                except Exception as e:
                    # Skip-and-log: keep the retry pass alive.
                    log.error("EP %s: retry fetch crashed: %s — keeping it for the next round",
                              ep["episode"], clean_error(e))
                    still_failed.append(ep)
                    continue
                if data and data.get("video_url"):
                    parsed = parse_episode_number(data.get("description", ""), data.get("title", ""))
                    data["_ep"] = parsed if parsed is not None else ep["episode"]
                    if series_title:
                        data["title"] = f"{series_title} EP.{data['_ep']}"
                    elif not data.get("title"):
                        data["title"] = f"EP.{data['_ep']}"
                    data["_page_url"] = ep_url
                    data.pop("_video_bytes", None)
                    videos.append(data)
                    log.info("Retry OK for EP %s (attempt %d, timeout %.0fs)",
                             ep["episode"], attempt, timeout)
                else:
                    still_failed.append(ep)
            failed = still_failed
            if failed:
                await asyncio.sleep(1.0)

    # Queue every still-failed episode for _insert's strict retry pass,
    # which will re-attempt downloads until MAX_UPLOAD_ATTEMPTS is reached.
    for ep in failed:
        log.warning("EP %s still failed after %d attempts — queued for upload retry pass",
                    ep["episode"], MAX_FETCH_ATTEMPTS)
        stub = {
            "title": f"{series_title} EP.{ep['episode']}" if series_title else f"EP.{ep['episode']}",
            "video_url": ep["url"],
            "_page_url": ep["url"],
            "_ep": ep["episode"],
            "description": "",
            "thumbnail": "",
            "duration": 1,
        }
        videos.append(stub)

    # ── Last-episode guarantee: if the expected FINAL episode (last tab's
    #    end, e.g. EP 60) never made it into the extracted list, queue the
    #    currently-open page's video so _insert's strict retry pass
    #    downloads it automatically.
    if last_ep_num and not any(v.get("_ep") == last_ep_num for v in videos):
        fill_url = last_ep_url
        if fill_url:
            log.warning("Final episode EP %d missing from extraction — queuing auto-fill from %s",
                        last_ep_num, fill_url)
            await _safe_edit(msg, f"⚠️ Сүүлийн анги (EP {last_ep_num}) дутуу байна — автоматаар нөхөж байна…")
            videos.append({
                "title": f"{series_title} EP.{last_ep_num}" if series_title else f"EP.{last_ep_num}",
                "video_url": fill_url,
                "_page_url": fill_url,
                "_ep": last_ep_num,
                "description": "",
                "thumbnail": "",
                "duration": 1,
            })

    # ── Completeness gate + auto-refill: if the extraction came back short
    #    of the official total (e.g. only 2 of 50), re-extract the series
    #    ONCE and merge the missing episode numbers in — the import below
    #    then fills 3..N in the same run and the bot can never report
    #    "everything already present" while the series is incomplete.
    if last_ep_num and len(episodes) < last_ep_num:
        seed = last_ep_url or (episodes[0]["url"] if episodes else None)
        if seed:
            await _safe_edit(
                msg,
                f"⚠️ Зөвхөн {len(episodes)}/{last_ep_num} анги гарлаа — "
                f"бүрэн жагсаалтыг дахин татаж байна…",
            )
            try:
                full = await asyncio.wait_for(
                    asyncio.to_thread(_extract_episodes, seed),
                    timeout=600,
                )
                if full and full[0].get("_meta"):
                    full.pop(0)
                merged: dict[int, dict] = {
                    int(e["episode"]): e for e in episodes
                    if 0 < int(e.get("episode") or 0) <= last_ep_num
                }
                for e in full:
                    num = int(e.get("episode") or 0)
                    if 0 < num <= last_ep_num:
                        merged[num] = e
                episodes = [merged[n] for n in sorted(merged)]
                log.info("Completeness refill: %d episodes after re-extraction", len(episodes))
            except Exception as e:
                log.warning("Completeness re-extraction failed: %s", clean_error(e))

    if not videos:
        await _safe_edit(msg, "❌ Could not extract any video sources from any of the %d episodes." % total)
        return

    # The OFFICIAL series cover (from extraction meta) wins as the poster —
    # _insert() never picks a random episode's thumbnail.
    if series_cover:
        for v in videos:
            v["_cover"] = v.get("_cover") or series_cover

    await _safe_edit(
        msg,
        f"✅ All {len(videos)}/{total} episodes extracted.\n"
        f"⬆️ Uploading to Supabase…"
    )
    # "Import complete" is only sent by _insert() after every episode has
    # been uploaded to Supabase.
    await _insert(msg, videos, force, official_total=last_ep_num)

    # ── Top-up loop (DB vs the OFFICIAL total): the conversation NEVER ends
    #    while the database holds fewer than N healthy episodes (e.g. only 2
    #    of 50).  FAST PATH: the bulk item_list API (few requests, cached
    #    across rounds) numbers every episode at once and the still-missing
    #    ones are fetched IN PARALLEL and imported immediately.  A full
    #    re-extraction only happens ONCE as a fallback when the bulk API
    #    cannot number the episodes.  Runs detached in the background, so
    #    rounds are patient (5→30s escalating) and the whole loop is bounded
    #    by a 30-minute budget + 2-empty-round early exit; the final sweep
    #    afterwards guarantees zero gaps.
    if last_ep_num and series_title:
        skey = slug_id(series_title)
        seed = last_ep_url or (episodes[0]["url"] if episodes else None)
        store = Store()
        topup_waits = [5, 10, 15, 20, 30]
        topup_deadline = time.monotonic() + 30 * 60  # whole top-up ≤ 30 min
        bulk_map: dict[int, str] | None = None
        fallback_tried = False
        stalls = 0
        for rnd in range(1, len(topup_waits) + 1):
            have = await _healthy_db_episodes(store, skey, last_ep_num)
            missing = sorted({n for n in range(1, last_ep_num + 1)} - have)
            if not missing or not seed:
                break
            if time.monotonic() > topup_deadline:
                log.warning("Top-up budget exhausted at round %d (DB %d/%d)",
                            rnd, len(have), last_ep_num)
                await _safe_edit(
                    msg,
                    f"⏸️ Бүрэн биш: {len(have)}/{last_ep_num} — TikTok удаашралтай. "
                    f"Линкийг дахин илгээвэл үлдсэнийг нөхнө.",
                )
                break
            missing_str = ", ".join(f"EP {n}" for n in missing[:8])
            if len(missing) > 8:
                missing_str += "…"
            await _safe_edit(
                msg,
                f"🔄 Дутуу {len(missing)} анги илэрлээ ({missing_str}) — "
                f"татаж нэмж байна ({rnd}/{len(topup_waits)})…",
            )
            imported_any = False

            # ── Fast path: bulk item_list API (one-time, cached) — ALL
            #    episode IDs + official numbers in a handful of requests,
            #    then the missing ones are fetched in parallel.
            if topup_sec_uid:
                try:
                    if bulk_map is None:
                        from tiktok_series import _api_item_list
                        items = await asyncio.to_thread(
                            _api_item_list, topup_username, topup_sec_uid)
                        bulk_map = {}
                        for it in items:
                            di = it.get("dramaInfo") or {}
                            if (topup_drama_id
                                    and str(di.get("dramaID") or "") != topup_drama_id):
                                continue
                            try:
                                ep = int(((di.get("DramaVideoData") or {})
                                          .get("EpisodeNumber")) or 0)
                            except (TypeError, ValueError):
                                ep = 0
                            vid = str(it.get("id") or "")
                            bw = re.search(r"(\d{15,25})", vid)
                            vid = bw.group(1) if bw else vid
                            if 1 <= ep <= last_ep_num and vid:
                                bulk_map.setdefault(ep, vid)
                        log.info("Bulk top-up map: %d/%d episodes numbered by API",
                                 len(bulk_map), last_ep_num)
                    cands = [(n, bulk_map[n]) for n in missing if n in bulk_map]
                    if cands:
                        fresh_vids: list[dict] = []
                        for i in range(0, len(cands), FETCH_CONCURRENCY):
                            chunk = cands[i:i + FETCH_CONCURRENCY]
                            results = await asyncio.gather(*[
                                _fetch_episode_source(
                                    f"https://www.tiktok.com/@{topup_username}/video/{vid}")
                                for _, vid in chunk
                            ])
                            for (n, _vid), data in zip(chunk, results):
                                if data and data.get("video_url"):
                                    page_url = (f"https://www.tiktok.com/"
                                                f"@{topup_username}/video/{_vid}")
                                    data["_ep"] = n
                                    data["title"] = f"{series_title} EP.{n}"
                                    data["_page_url"] = page_url
                                    data["description"] = ""
                                    data.pop("_video_bytes", None)
                                    fresh_vids.append(data)
                        if fresh_vids:
                            await _insert(msg, fresh_vids, force,
                                          official_total=last_ep_num)
                            imported_any = True
                            log.info("Top-up fast path imported %d episodes",
                                     len(fresh_vids))
                except Exception as e:
                    log.warning("Top-up bulk path failed: %s", clean_error(e))

            # ── Fallback: ONE full re-extraction (round 1 only) when the
            #    bulk API did not number the episodes (non-drama account).
            if not imported_any and not fallback_tried:
                fallback_tried = True
                try:
                    fresh = await asyncio.wait_for(
                        asyncio.to_thread(_extract_episodes, seed),
                        timeout=600,
                    )
                    if fresh and fresh[0].get("_meta"):
                        fresh.pop(0)
                    fresh_vids = []
                    already = await _healthy_db_episodes(store, skey, last_ep_num)
                    for e in fresh:
                        n = int(e.get("episode") or 0)
                        if 0 < n <= last_ep_num and n in missing and n not in already:
                            fresh_vids.append({
                                "title": f"{series_title} EP.{n}",
                                "video_url": e["url"],
                                "_page_url": e["url"],
                                "_ep": n,
                                "description": "",
                                "thumbnail": "",
                                "duration": 1,
                            })
                    if fresh_vids:
                        await _insert(msg, fresh_vids, force,
                                      official_total=last_ep_num)
                        imported_any = True
                except Exception as e:
                    log.warning("Top-up fallback re-extraction failed: %s",
                                clean_error(e))
            log.info("Top-up round %d done (imported_any=%s)", rnd, imported_any)
            if imported_any:
                stalls = 0
            else:
                stalls += 1
                if stalls >= 2 and rnd < len(topup_waits):
                    have = await _healthy_db_episodes(store, skey, last_ep_num)
                    await _safe_edit(
                        msg,
                        f"⚠️ Бүрэн биш: {len(have)}/{last_ep_num} — TikTok "
                        f"одоогоор удаан. Линкийг дахин илгээвэл үлдсэнийг нөхнө.",
                    )
                    break
            if not imported_any and rnd < len(topup_waits):
                await asyncio.sleep(topup_waits[rnd - 1])


# ── Insertion ───────────────────────────────────────────────────────────────

async def _healthy_db_episodes(store: "Store", skey: str, limit: int | None = None) -> set[int]:
    """Distinct episode numbers 1..limit that are FULLY playable in the DB
    (real video_url, not pending, storage object healthy).  This is the
    single source of truth for the "гүйцсэн үү?" check — never a guess.
    Every DB/storage call is watchdog-guarded (never blocks the loop)."""
    have: set[int] = set()
    try:
        rows = await _db_call(store.get_series_episodes, skey)
        if rows is None:
            return have
        for row in rows:
            ep = int(row.get("episode_number") or 0)
            if not ep or (limit and ep > limit):
                continue
            if not str(row.get("video_url") or ""):
                continue
            if store.has_status and str(row.get("status") or "") == "pending":
                continue
            broken = await _db_call(store.is_episode_video_broken, skey, ep)
            if broken:
                continue
            have.add(ep)
    except Exception as e:
        log.warning("Healthy-episode query for %s failed: %s", skey, clean_error(e))
    return have

def _log_memory(tag: str) -> None:
    """Log the process RSS so RAM growth is visible in Render logs.

    Reads /proc/self/status (Linux — Render); a silent no-op elsewhere.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    log.info("[mem:%s] RSS=%.1f MB", tag, kb / 1024)
                    return
    except Exception:
        pass

async def _insert(msg, videos: list[dict], force: bool = False,
                  official_total: int | None = None) -> None:
    store = Store()
    groups: dict[str, list[dict]] = {}
    for v in videos:
        series_title, ep_num = parse_episode(v["title"])
        v["_series"] = v.get("_series") or series_title
        key = slug_id(v["_series"])
        # Pre-parsed real episode number (description/title text) wins;
        # otherwise fall back to the title parse.
        v["_ep"] = v.get("_ep") if v.get("_ep") is not None else ep_num
        groups.setdefault(key, []).append(v)

    created = 0
    inserted = 0
    skipped = 0
    overwritten = 0
    failed: list[tuple] = []
    inserted_eps: list[tuple] = []
    touched_titles: dict[str, str] = {}
    # Episodes bulk-registered as pending by THIS import (before upload).
    pre_registered: set[tuple[str, int]] = set()

    total_groups = len(groups)
    total_episodes = sum(len(v) for v in groups.values())
    processed = 0

    for idx, (skey, vlist) in enumerate(groups.items(), 1):
        stitle = vlist[0]["_series"]
        desc = vlist[0].get("description", stitle)

        # If series title is garbage, use @author instead
        if is_garbage_title(stitle):
            author = vlist[0].get("username", "") or vlist[0].get("uploader", "")
            if author:
                stitle = f"@{author}"
                skey = slug_id(stitle)
                for v in vlist:
                    v["_series"] = stitle
        touched_titles[skey] = stitle

        # ── Poster selection: the series' OFFICIAL cover (extraction meta)
        #    wins; otherwise Episode 1's cover ONLY — never a random
        #    episode's thumbnail.
        thumb = next((v.get("_cover") for v in vlist if v.get("_cover")), "")
        if not thumb:
            ep1 = next((v for v in vlist if v.get("_ep") == 1), None)
            if ep1 and ep1.get("thumbnail"):
                thumb = ep1["thumbnail"]
            elif await _db_call(store.episode_exists, skey, 1):
                thumb = await _db_call(store.get_episode_thumbnail, skey, 1) or ""
            else:
                thumb = ""
        if not thumb:
            log.warning("No official cover and no EP 1 cover for %s — falling back to first video thumbnail", stitle)
            thumb = vlist[0].get("thumbnail", "")

        await _safe_edit(
            msg,
            f"📦 Series [{idx}/{total_groups}]: *{stitle}*",
            parse_mode="Markdown",
        )
        await _db_call(store.upsert_series, skey, stitle, DEFAULT_GENRE, desc, thumb)
        created += 1

        # ── Order by the REAL episode number, not the download order ────
        # Duplicate parsed numbers (e.g. two captions both say "EP.1")
        # are bumped to the next free number so nothing is lost.
        known = sorted([v for v in vlist if v["_ep"] is not None], key=lambda v: v["_ep"])
        unknown = [v for v in vlist if v["_ep"] is None]
        used: set[int] = set()
        for v in known:
            ep = v["_ep"]
            while ep in used:
                ep += 1
                v["_ep"] = ep
                log.warning("Duplicate episode number in %s — bumped to %d", stitle, ep)
            used.add(ep)
        next_num = (max(used) + 1) if used else 1
        for v in unknown:
            while next_num in used:
                next_num += 1
            v["_ep"] = next_num
            used.add(next_num)
            next_num += 1
        ordered = known + unknown

        # ── Bulk pre-registration (ONE upsert call per series): every
        #    episode that is not already healthy is written into the DB as
        #    PENDING right away, so the series (and its full episode list)
        #    appears in the app instantly.  The upload loop below then fills
        #    in the real video_url row by row.
        bulk_rows: list[dict] = []
        for v in ordered:
            ep = v["_ep"]
            if (await _db_call(store.episode_exists, skey, ep)
                    and not await _db_call(store.is_episode_pending, skey, ep)
                    and not await _db_call(store.is_episode_video_broken, skey, ep)):
                continue
            source = v.get("_page_url") or v.get("video_url") or ""
            row = {
                "id": f"ep-{skey}-{ep}",
                "series_id": skey,
                "episode_number": ep,
                "title": v.get("title") or f"EP.{ep}",
                "description": source if not store.has_status else (v.get("description") or ""),
                "video_url": "",
                "thumbnail_url": "",
                "duration": 0,
                "is_free": ep <= DEFAULT_FREE_FIRST,
            }
            if store.has_status:
                row["status"] = "pending"
                row["source_url"] = source
            bulk_rows.append(row)
            pre_registered.add((skey, ep))
        if bulk_rows:
            await _db_call(store.bulk_upsert_pending, skey, bulk_rows)

        # ── Work list: episodes that actually need a download+upload ────
        # (skip/pending/broken checks stay sequential — they are fast DB
        # reads; the HEAVY part below runs in parallel.)
        todo: list[dict] = []
        for v in ordered:
            ep = v["_ep"]
            processed += 1
            if await _db_call(store.episode_exists, skey, ep):
                if (skey, ep) in pre_registered:
                    pass  # bulk-registered by this import — just upload it
                # Skip ONLY healthy existing episodes.  Pending (incomplete/
                # retrying) and broken (0-byte/expired) episodes are never
                # treated as duplicates — they are re-downloaded & completed.
                elif (not force
                      and not await _db_call(store.is_episode_pending, skey, ep)
                      and not await _db_call(store.is_episode_video_broken, skey, ep)):
                    skipped += 1
                    continue
                else:
                    overwritten += 1
                log.info(
                    "EP %s of %s exists but is %s — re-importing (overwrite)",
                    ep, stitle, "forced" if force else ("pending/broken" if (skey, ep) not in pre_registered else "just pre-registered"),
                )
            todo.append(v)

        # ── PARALLEL upload pipeline ────────────────────────────────────
        # IMPORT_WORKERS episodes at a time: CDN download → storage POST →
        # thumbnail → DB row.  A 50-episode series finishes in minutes
        # instead of half an hour; each worker holds ≤1 video in RAM.
        sem = asyncio.Semaphore(max(1, IMPORT_WORKERS))
        done_count = 0

        async def _process_one(v: dict) -> None:
            nonlocal done_count, inserted
            ep = v["_ep"]
            async with sem:
                try:
                    preloaded = v.get("_video_bytes") or None
                    video_url = await _upload_video_watchdog(store, skey, ep, v["video_url"], preloaded)
                    thumb_url = v["thumbnail"]
                    if video_url and thumb_url:
                        thumb_url = await _db_call(store.upload_thumbnail, skey, ep, thumb_url)
                except Exception as e:
                    # Skip-and-log: an upload crash must never abort the series —
                    # mark the episode failed and continue with the next one.
                    log.error("Upload crashed for EP %s of %s: %s — queued for retry",
                              ep, stitle, clean_error(e))
                    v.pop("_video_bytes", None)
                    failed.append((skey, ep, stitle, v))
                    return
                # Free the downloaded bytes NOW — keeping every episode's video in
                # RAM for the whole import would pin 10–50 MB × N episodes (OOM).
                v.pop("_video_bytes", None)

                if not video_url:
                    # Upload failed (download error or content < MIN_VIDEO_SIZE).
                    # Never store an empty URL — queue for the strict retry pass.
                    log.warning("Upload failed for EP %s of %s — queued for retry", ep, stitle)
                    failed.append((skey, ep, stitle, v))
                    return

                v["video_url"] = video_url
                v["thumbnail"] = thumb_url
                await _db_call(store.upsert_episode, skey, ep, v, ep <= DEFAULT_FREE_FIRST)
                inserted += 1
                inserted_eps.append((skey, ep, stitle, v))

            done_count += 1
            # Per-episode memory hygiene: return video bytes to the OS and
            # log RSS so RAM growth stays visible in the Render logs.
            gc.collect()
            _log_memory(f"after EP {ep} of {stitle}")

        if todo:
            tasks = [asyncio.create_task(_process_one(v)) for v in todo]
            for fut in asyncio.as_completed(tasks):
                await fut
                if done_count and (done_count % 5 == 0 or done_count == len(todo)):
                    await _safe_edit(
                        msg,
                        f"📦 [{idx}/{total_groups}] *{stitle}* — "
                        f"{done_count}/{len(todo)} анги байршуулж байна…",
                        parse_mode="Markdown",
                    )

    # ── Strict retry pass: re-download + re-upload every failed episode,
    #    up to MAX_UPLOAD_ATTEMPTS rounds.  Each attempt runs the full
    #    fallback chain (yt-dlp → ssstik → TikWM).
    if failed:
        for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
            if not failed:
                break
            failed_nums = ", ".join(f"EP {e[1]}" for e in failed[:6])
            if len(failed) > 6:
                failed_nums += "…"
            await _safe_edit(
                msg,
                f"🔄 Retry {attempt}/{MAX_UPLOAD_ATTEMPTS} — re-downloading: {failed_nums}…",
            )
            still: list[tuple] = []
            for fkey, fep, ftitle, fv in failed:
                try:
                    data = await _fetch_episode_source(
                        fv.get("_page_url") or fv["video_url"], fv.get("title"))
                    if not data:
                        still.append((fkey, fep, ftitle, fv))
                        continue
                    rvideo = await _upload_video_watchdog(
                        store, fkey, fep, data["video_url"], data.get("_video_bytes") or None)
                    data.pop("_video_bytes", None)
                except Exception as e:
                    log.error("Retry crashed for EP %s of %s: %s — keeping it for the next round",
                              fep, ftitle, clean_error(e))
                    still.append((fkey, fep, ftitle, fv))
                    continue
                if rvideo:
                    fv["video_url"] = rvideo
                    await _db_call(store.upsert_episode, fkey, fep, fv, fep <= DEFAULT_FREE_FIRST)
                    inserted += 1
                    inserted_eps.append((fkey, fep, ftitle, fv))
                    log.info("Retry OK for EP %s of %s (round %d)", fep, ftitle, attempt)
                else:
                    still.append((fkey, fep, ftitle, fv))
            failed = still
            if failed:
                await asyncio.sleep(3)

    # ── Auto-clean before the success message: verify every inserted
    #    episode's storage object is healthy (non-zero size, readable).
    #    Broken ones are re-downloaded & re-uploaded automatically.
    broken_eps: list[tuple] = []
    if inserted_eps:
        await _safe_edit(msg, "🔍 Шалгаж байна: upload-д орсон видеонууд…")
        # Parallel health check — one storage HEAD per episode, N at a time.
        bsem = asyncio.Semaphore(max(1, IMPORT_WORKERS))

        async def _check_broken(entry: tuple) -> tuple | None:
            bk, bep, bt, bv = entry
            async with bsem:
                broken = await _db_call(store.is_episode_video_broken, bk, bep)
            if broken:
                log.warning("Auto-clean: EP %s of %s storage object broken — re-importing", bep, bt)
                return entry
            return None

        results = await asyncio.gather(*[_check_broken(e) for e in inserted_eps])
        broken_eps = [r for r in results if r is not None]
        if broken_eps:
            for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
                if not broken_eps:
                    break
                still_broken: list[tuple] = []
                for bkey, bep, btitle, bv in broken_eps:
                    try:
                        data = await _fetch_episode_source(
                            bv.get("_page_url") or bv["video_url"], bv.get("title"))
                        if not data:
                            still_broken.append((bkey, bep, btitle, bv))
                            continue
                        rvideo = await _upload_video_watchdog(
                            store, bkey, bep, data["video_url"], data.get("_video_bytes") or None)
                        data.pop("_video_bytes", None)
                    except Exception as e:
                        log.error("Auto-clean crashed for EP %s of %s: %s — keeping it for the next round",
                                  bep, btitle, clean_error(e))
                        still_broken.append((bkey, bep, btitle, bv))
                        continue
                    if rvideo:
                        bv["video_url"] = rvideo
                        await _db_call(store.upsert_episode, bkey, bep, bv, bep <= DEFAULT_FREE_FIRST)
                        log.info("Auto-clean OK for EP %s of %s (round %d)", bep, btitle, attempt)
                    else:
                        still_broken.append((bkey, bep, btitle, bv))
                broken_eps = still_broken
                if broken_eps:
                    await asyncio.sleep(3)

    # ── Persist PENDING state ──────────────────────────────────────────────
    # Episodes that still failed (download or storage verification) are
    # written to the DB as pending (empty video_url + stored TikTok source
    # URL).  They are NEVER skipped as duplicates on the next import — the
    # cleanup queue below and any future import of this series complete them.
    for fkey, fep, ftitle, fv in failed + broken_eps:
        await _db_call(store.upsert_episode_pending, fkey, fep, fv)
        log.warning("EP %s of %s marked PENDING in DB (auto-complete on next import)", fep, ftitle)

    # ── Auto cleanup queue: collect EVERY pending/incomplete episode of
    #    every touched series (including older ones not in this import's
    #    batch) and finish downloading them all in one final sweep.
    cleanup_left: list[tuple] = []
    if groups:
        already_done = {(f[0], f[1]) for f in failed + broken_eps + inserted_eps}
        pending_sources: list[tuple] = []
        for skey, stitle in touched_titles.items():
            all_rows = await _db_call(store.get_series_episodes, skey) or []
            for row in all_rows:
                ep = int(row.get("episode_number") or 0)
                if not ep or (skey, ep) in already_done:
                    continue
                vid_url = str(row.get("video_url") or "")
                is_pending = (not vid_url) or (store.has_status and str(row.get("status") or "") == "pending")
                if not is_pending:
                    # Fast path: healthy storage-backed rows are trusted.
                    # CDN-backed rows are re-checked; when this import had
                    # failures, sweep the storage rows too.
                    if vid_url.startswith(f"{SUPABASE_URL}/storage/v1/object/public/"):
                        if not (failed or broken_eps):
                            continue
                    if not store.is_episode_video_broken(skey, ep):
                        continue
                if store.has_status:
                    src = str(row.get("source_url") or "")
                elif is_pending:
                    src = str(row.get("description") or "")
                else:
                    src = ""
                if not src:
                    continue
                pending_sources.append((skey, ep, stitle, src, str(row.get("title") or f"EP.{ep}")))
        if pending_sources:
            await _safe_edit(
                msg,
                f"🧹 Auto cleanup: {len(pending_sources)} дутуу анги илэрлээ — нэг дор нөхөж байна…",
            )
            for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
                if not pending_sources:
                    break
                still_pending: list[tuple] = []
                for skey, ep, stitle, src, title in pending_sources:
                    try:
                        data = await _fetch_episode_source(src, title)
                        if not data or not data.get("video_url"):
                            still_pending.append((skey, ep, stitle, src, title))
                            continue
                        rvideo = await _upload_video_watchdog(
                            store, skey, ep, data["video_url"], data.get("_video_bytes") or None)
                        data.pop("_video_bytes", None)
                    except Exception as e:
                        log.error("Cleanup crashed for EP %s of %s: %s — keeping it for the next round",
                                  ep, stitle, clean_error(e))
                        still_pending.append((skey, ep, stitle, src, title))
                        continue
                    if rvideo:
                        data["_page_url"] = src
                        data["title"] = title
                        await _db_call(store.upsert_episode, skey, ep, data, ep <= DEFAULT_FREE_FIRST)
                        inserted += 1
                        inserted_eps.append((skey, ep, stitle, data))
                        log.info("Auto cleanup OK: EP %s of %s (round %d)", ep, stitle, attempt)
                    else:
                        still_pending.append((skey, ep, stitle, src, title))
                pending_sources = still_pending
                if pending_sources:
                    await asyncio.sleep(3)
        cleanup_left = pending_sources

    final_failures = failed + broken_eps + cleanup_left

    # ── Completeness gate vs the OFFICIAL total: "Бүх анги аль хэдийн
    #    оруулсан" may only be claimed when the database actually holds a
    #    healthy row for EVERY episode 1..N (e.g. all 50).  With only 2 of
    #    50 present the import is reported as partial — never as complete.
    db_have: set[int] = set()
    db_total_known = bool(official_total) and len(groups) == 1
    if db_total_known:
        (skey, _), = groups.items()
        db_have = await _healthy_db_episodes(store, skey, official_total)
        log.info("DB completeness for %s: %d/%d official episodes healthy",
                 skey, len(db_have), official_total)

    fail_eps = ", ".join(f"EP {e[1]}" for e in final_failures[:10])
    titles = ", ".join(touched_titles.values()) or "unknown"
    if final_failures:
        if len(final_failures) > 10:
            fail_eps += "…"
        status_line = (
            f"⚠️ *Бүрэн биш:* {len(final_failures)} анги амжилтгүй ({fail_eps}).\n"
            f"Киноны нэр: {titles}, Нийт оруулав: {inserted} анги\n"
            f"Амжилтгүй ангиудыг 'pending' төлөвт тэмдэглэсэн — дараагийн импортод автоматаар нөхөгдөнө."
        )
    else:
        incomplete = db_total_known and len(db_have) < official_total
        if inserted == 0 and skipped > 0:
            if incomplete:
                status_line = (
                    f"⚠️ *Бүрэн биш:* өгөгдлийн санд зөвхөн "
                    f"{len(db_have)}/{official_total} анги л байна — "
                    f"үлдсэн {official_total - len(db_have)} ангийг нөхөхөд "
                    f"цувралын дурын видеог дахин илгээнэ үү."
                )
            elif db_total_known:
                status_line = (
                    f"ℹ️ *Бүх анги аль хэдийн оруулсан байна* (нийт {skipped} ангийг давхар шалгаж, "
                    f"шинийг нэмэх шаардлагагүй байлаа).\n"
                    f"Киноны нэр: {titles}"
                )
            else:
                # No official total available (regular playlist account) —
                # never claim a series is complete without that guarantee.
                status_line = (
                    f"ℹ️ {skipped} анги давхар шалгагдсан — шинэ анги нэмэгдээгүй. "
                    f"Киноны нэр: {titles}"
                )
        elif incomplete:
            status_line = (
                f"⚠️ *Бүрэн биш:* зөвхөн {len(db_have)}/{official_total} анги нь "
                f"бүрэн орсон ({inserted} анги энэ удаа оруулав) — "
                f"үлдсэн {official_total - len(db_have)} ангийг нөхөхөд "
                f"цувралын дурын видеог дахин илгээнэ үү."
            )
        else:
            status_line = f"✅ *Амжилттай орууллаа!* Киноны нэр: {titles}, Нийт оруулав: {inserted} анги."

    dup_warn = (
        f"\n⚠️ {skipped} анги давхар байсан тул алгассан (зөвхөн бүрэн, эрүүл ангиуд)."
        if skipped else ""
    )
    overwrite_warn = (
        f"\n♻️ {overwritten} анги дахин импортлогдсон (дутуу/эвдэрхий/force)."
        if overwritten else ""
    )
    await _safe_edit(
        msg,
        f"{status_line}\n\n"
        f"• Цуврал: {created}\n"
        f"• Анги оруулсан: {inserted}\n"
        f"• Давхар алгассан: {skipped}{dup_warn}\n"
        f"• Дахин импортлосон: {overwritten}{overwrite_warn}\n\n"
        f"Дараагийн удаа цувралын дурын видеог илгээнэ үү — үлдсэн дутуу ангиуд автоматаар нөхөгдөнө.",
        parse_mode="Markdown",
    )


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        print("Get one from https://t.me/BotFather")
        sys.exit(1)
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: EXPO_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
        sys.exit(1)

    # ── Env sanity check: a wrong/missing host shows a CLEAR message now
    #    instead of a cryptic `[Errno -2] Name or service not known` later.
    try:
        from urllib.parse import urlparse as _up
        pu = _up(SUPABASE_URL)
        if not pu.scheme or not pu.hostname or pu.hostname.count(".") < 1:
            print(f"ERROR: EXPO_PUBLIC_SUPABASE_URL is not a valid URL: '{SUPABASE_URL}'")
            sys.exit(1)
        log.info("Supabase host: %s (project: %s)", pu.hostname,
                 pu.hostname.split(".")[0])
    except Exception as e:
        print(f"ERROR: EXPO_PUBLIC_SUPABASE_URL unparseable: {SUPABASE_URL} ({e})")
        sys.exit(1)

    # Loud early warning when the Supabase project is paused/deleted
    # (its DNS record disappears → every DB call fails with Errno -2).
    if _supabase_dns_down():
        print("=" * 60)
        print("WARNING: Supabase host does NOT resolve!")
        print(f"  Host: {_up(SUPABASE_URL).hostname}")
        print("  The project is most likely PAUSED (free tier pauses")
        print("  after inactivity) or deleted.")
        print("  Fix: supabase.com/dashboard -> select project -> 'Restore project'.")
        print("  The bot will start, but ALL imports will fail until restored.")
        print("=" * 60)
        log.warning("Supabase DNS unresolvable — project paused? Imports will fail until restored")
    if os.getenv("DATABASE_URL"):
        log.info("DATABASE_URL set — app verifies/inits its own schema on first run")
    else:
        log.info("DATABASE_URL not set — Supabase RestAPI is used exclusively (fine)")
    if os.getenv("RAPIDAPI_KEY"):
        log.info("RAPIDAPI fallback configured")
    else:
        log.info("RAPIDAPI_KEY not set — yt-dlp + web scraping only")

    # Verify storage bucket at startup
    db = create_client(SUPABASE_URL, SUPABASE_KEY)
    if _ensure_videos_bucket(db):
        log.info("✓ Supabase Storage bucket '%s' is ready", STORAGE_BUCKET)
    else:
        log.warning("! Storage bucket '%s' unavailable — will store tikcdn.io URLs directly", STORAGE_BUCKET)

    async def _post_init(app: Application) -> None:
        """Run once per polling start: delete any stale webhook.

        A leftover webhook URL (set by an earlier experiment or a previous
        deploys) makes Telegram reject `getUpdates` with 409 Conflict.
        Clearing it here — on the SAME event loop that polling will use —
        is the only reliable spot before run_polling starts.
        """
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            log.info("Stale webhook cleared before polling")
        except Exception as e:
            log.warning("Could not clear webhook before polling: %s", clean_error(e))
        log.info("Bot starting — commit %s, python %s, workers=%d",
                 BOT_VERSION, sys.version.split()[0], IMPORT_WORKERS)
        # Self-healing across restarts: pending episodes left by a crash or
        # a Render redeploy are re-imported in the background automatically.
        try:
            asyncio.get_running_loop().create_task(_startup_pending_sweep())
        except Exception as e:
            log.warning("Startup sweep scheduling failed: %s", clean_error(e))

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(300)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("series", cmd_series))
    app.add_handler(CommandHandler("single", cmd_single))
    app.add_handler(CommandHandler("force", cmd_force))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Never let a handler crash silently — log it and tell the user."""
        log.error("Unhandled handler error: %s", clean_error(context.error))
        log.exception("Full traceback", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    f"❌ Алдаа: {clean_error(context.error)[:300]}\n"
                    "Сүлжээ тасарсан/хугацаа хэтэрсэн байж болно — дахин илгээж үзээрэй."
                )
            except Exception as e2:
                log.error("Failed to send error notification: %s", clean_error(e2))

    app.add_error_handler(_error_handler)
    log.info("🤖 TikTok → Supabase bot running…")

    # ── Polling loop with single-instance protection ──────────────────────
    # Render's rolling deploys briefly run TWO containers with the same
    # bot token → Telegram throws `Conflict: terminated by other getUpdates
    # request`.  Instead of crashing, the bot waits with an escalating
    # backoff (60s → 300s) until the old instance has released the polling
    # lock.  `drop_pending_updates` skips stale updates queued while down.
    #
    # IMPORTANT: polling MUST run on the MAIN thread — an asyncio loop in a
    # background thread crashes with `RuntimeError: set_wakeup_fd only works
    # in main thread` on Python 3.12.  The Render health server already runs
    # in its own daemon thread (see start_health_server above).
    backoff = 60
    while True:
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            break  # graceful shutdown (KeyboardInterrupt / /stop)
        except Conflict as e:
            log.warning(
                "Telegram Conflict (another instance polling) — retrying in %ds: %s",
                backoff, e,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
