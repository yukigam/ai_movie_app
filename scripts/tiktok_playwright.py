"""
Playwright utility — persistent TikTok session & episode extraction.

Provides a persistent browser context (saved to disk) so TikTok
recognises the session as a logged-in human.  Functions here are used
by both ``telegram_bot.py`` and ``tiktok_webhook.py``.

Usage
-----
Step 1 – One-time login::

    python scripts/tiktok_playwright.py --login

    → A browser window opens.  Log in to TikTok manually, then close it.

Step 2 – Extract episodes::

    python scripts/tiktok_playwright.py --url \\
        https://www.tiktok.com/@shortdramatime/video/7666143423493164308

    → Prints JSON list of episode URLs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import socket
import sys
import time as time_module
from pathlib import Path

log = logging.getLogger("ttpw")

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = str(PROJECT_ROOT / ".playwright_data")  # persistent profile on disk
STORAGE_STATE = str(PROJECT_ROOT / "playwright_storage.json")

# Hard cap on how long ONE sidebar episode click may take (scroll → click →
# wait for video change).  If TikTok's page JS hangs, asyncio.wait_for kills
# the work and extraction moves on to the next episode.
EPISODE_PROCESS_TIMEOUT = 20.0

# Hard cap for processing ONE pagination tab (clicking through all its
# episodes).  A wedged tab aborts and the browser page is recovered.
TAB_PROCESS_TIMEOUT = 300.0


def _storage_exists() -> bool:
    return os.path.isfile(STORAGE_STATE)


async def _random_sleep(min_s: float = 3.0, max_s: float = 7.0):
    """Sleep a random interval between *min_s* and *max_s* seconds.
    
    Adds jitter to avoid TikTok rate-limit detection (bot-like timing).
    """
    delay = random.uniform(min_s, max_s)
    log.debug("Sleeping %.1fs (anti-rate-limit jitter)", delay)
    await asyncio.sleep(delay)


# ── Browser helpers ──────────────────────────────────────────────────────────

async def create_playwright_context(p, headless: bool = True):
    """Create a Playwright browser context with saved storage state.
    
    *p* is the Playwright async instance (from ``async with async_playwright() as p:``).
    
    If *STORAGE_STATE* exists, cookies + localStorage are restored so TikTok
    sees a previously-logged-in session.
    
    Returns ``(browser, context)``.
    """
    browser = await p.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
        ],
    )

    # Use a current Chrome UA (2026)
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1400, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        storage_state=STORAGE_STATE if _storage_exists() else None,
    )

    # Remove webdriver detection + mimic real browser
    await context.add_init_script("""
        // Core anti-detection
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });
        // Hardware fingerprint spoofing
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
        Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 1 });
        // Screen properties
        Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
        Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
        // Connection
        if (navigator.connection) {
            Object.defineProperty(navigator.connection, 'rtt', { get: () => 100 });
            Object.defineProperty(navigator.connection, 'downlink', { get: () => 10 });
            Object.defineProperty(navigator.connection, 'effectiveType', { get: () => '4g' });
        }
        // Override permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) => (
            params.name === 'notifications' ? Promise.resolve({state: 'prompt'}) : originalQuery(params)
        );
        // Override chrome runtime
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {},
        };
        // WebGL vendor spoofing
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter.call(this, p);
        };
        // Canvas fingerprint noise
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type) {
            const canvas = this;
            const result = origToDataURL.call(canvas, type);
            // Add subtle noise to fingerprint
            if (result.includes('data:image/png')) {
                return result.slice(0, -10) + 'abcdef=';
            }
            return result;
        };
        // Timezone & locale
        Object.defineProperty(Intl, 'DateTimeFormat', {
            value: new Proxy(Intl.DateTimeFormat, {
                apply: function(target, thisArg, args) {
                    if (!args || !args[0]) args = ['en-US'];
                    return new target(...args);
                }
            })
        });
    """)

    return browser, context


async def save_storage(context):
    """Persist the current context's cookies + localStorage to disk."""
    try:
        state = await context.storage_state(path=STORAGE_STATE)
        log.info("Saved storage state to %s (%d cookies)",
                 STORAGE_STATE, len(state.get("cookies", [])))
        return True
    except Exception as e:
        log.warning("Failed to save storage state: %s", e)
        return False


# ── Episode extraction ───────────────────────────────────────────────────────

async def _wait_for_grid(page, timeout: int = 30) -> list:
    """Wait up to *timeout* seconds for an episode list/sidebar to appear.

    Returns a list of clickable child coordinates {index, x, y, text} from
    the correctly-detected episode grid, or [] if not found.
    """
    import time
    deadline = time.monotonic() + timeout
    class_patterns = ['DivEpisodeGrid', 'EpisodeList', 'EpisodeSidebar',
                      'DivPlaylistContainer', 'DivSeriesContainer',
                      'DivEpisodeContainer', 'EpisodeItem']
    while time.monotonic() < deadline:
        # Strategy 1: class name patterns (returns child coords from the grid)
        found = await page.evaluate(f"""() => {{
            const patterns = {json.dumps(class_patterns)};
            for (const el of document.querySelectorAll('*')) {{
                const c = el.className || '';
                if (typeof c === 'string' && patterns.some(p => c.includes(p))) {{
                    if (el.children.length > 0) {{
                        const items = [];
                        for (let i = 0; i < el.children.length; i++) {{
                            const child = el.children[i];
                            const r = child.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && r.top > 0 && r.top < window.innerHeight) {{
                                items.push({{
                                    index: i,
                                    x: Math.round(r.left + r.width / 2),
                                    y: Math.round(r.top + r.height / 2),
                                    text: (child.textContent || '').trim().slice(0, 40),
                                }});
                            }}
                        }}
                        return items;
                    }}
                }}
            }}
            return [];
        }}""")
        if isinstance(found, list) and len(found) >= 4:
            log.info("Episode grid/sidebar appeared with %d clickable children (class match)", len(found))
            return found

        # Strategy 2: look for containers with many numbered child elements in right sidebar area
        btn_count = await page.evaluate("""() => {
            const threshold = window.innerWidth * 0.55;
            let count = 0;
            for (const el of document.querySelectorAll('[class*="Episode"],[class*="episode"],[class*="series"],[class*="playlist"]')) {
                const rect = el.getBoundingClientRect();
                if (rect.left > threshold && rect.width > 0) {
                    const text = (el.textContent || '').trim();
                    const nums = text.match(/\\d+/g);
                    if (nums) count += nums.length;
                }
            }
            return count;
        }""")
        if isinstance(btn_count, int) and btn_count > 5:
            log.info("Found ~%d potential episode buttons via sidebar scan", btn_count)

        # Strategy 3: look for any visible elements with just a number (1-200) in the right 55%
        has_numbers = await page.evaluate("""() => {
            const threshold = window.innerWidth * 0.55;
            let found = 0;
            for (const el of document.querySelectorAll('button, a, div, span')) {
                const t = (el.textContent || '').trim();
                const num = parseInt(t, 10);
                if (!isNaN(num) && num > 0 && num <= 200 && String(num) === t) {
                    const rect = el.getBoundingClientRect();
                    if (rect.left > threshold && rect.width > 0 && rect.height > 0) {
                        found++;
                    }
                }
            }
            return found;
        }""")
        if isinstance(has_numbers, int) and has_numbers >= 2:
            log.info("Found %d numbered episode buttons in sidebar area", has_numbers)

        await asyncio.sleep(1.5)
    return []


async def _get_current_video_id(page) -> str | None:
    """Extract the video ID from the current page URL or rehydration data."""
    m = re.search(r"/video/(\d+)", page.url)
    if m:
        return m.group(1)
    vid = await page.evaluate("""() => {
        const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (!s) return null;
        try {
            const d = JSON.parse(s.textContent);
            const vd = d.__DEFAULT_SCOPE__?.['webapp.video-detail'];
            return vd?.itemInfo?.itemStruct?.id || null;
        } catch(e) { return null; }
    }""")
    return vid


async def _get_series_title(page) -> str | None:
    """Extract the REAL series/playlist title from the page.

    Priority order:
      1. Rehydration data — series/playlist objects carrying a title/name
      2. DOM — ``data-e2e`` series-title elements, ``/series/`` anchors,
         SeriesTitle class patterns, "Episodes" sidebar header headings
      3. "Drama ⭐ Watch all N episodes" button text (junk stripped)
      4. Video description first line (caption cleaned of episode markers)
    """
    title = await page.evaluate("""() => {
        const seen = new Set();
        const clean = (t) => {
            t = (t || '').replace(/[\\u{1F000}-\\u{1FAFF}\\u2600-\\u27BF]/gu, '').replace(/\\s+/g, ' ').trim();
            return t;
        };
        const ok = (t) => {
            if (!t || t.length < 2 || t.length > 200) return false;
            if (seen.has(t)) return false;
            if (/^(drama|series|playlist|episodes?)$/i.test(t)) return false;
            if (/^(ep(?:isode)?\\.?\\s*\\d+|\\d+\\s*ep(?:isodes)?|watch\\s+all)/i.test(t)) return false;
            return true;
        };

        // Method 1: rehydration data — deep search series/playlist objects for a title
        const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (s) {
            try {
                const d = JSON.parse(s.textContent);
                const scope = d.__DEFAULT_SCOPE__ || {};
                const found = [];
                (function walk(obj, path) {
                    if (!obj || typeof obj !== 'object') return;
                    if (Array.isArray(obj)) {
                        for (let i = 0; i < obj.length; i++) walk(obj[i], path + '[' + i + ']');
                        return;
                    }
                    if (/series|playlist|collection|drama/.test(path.toLowerCase())) {
                        const t = obj.title || obj.name || obj.seriesTitle || obj.playlistTitle;
                        if (typeof t === 'string' && t.trim()) found.push(t.trim());
                    }
                    for (const k of Object.keys(obj)) {
                        walk(obj[k], path ? path + '.' + k : k);
                    }
                })(scope, '');
                for (const t of found) {
                    const c = clean(t);
                    if (ok(c) && !/watch\\s+all/i.test(c)) return c;
                }
            } catch (e) {}
        }

        // Method 2: DOM series-title elements (data-e2e)
        for (const el of document.querySelectorAll('[data-e2e]')) {
            const e2e = (el.getAttribute('data-e2e') || '').toLowerCase();
            if (e2e.includes('series-title') || e2e.includes('playlist-title') || e2e.includes('collection-title')) {
                const c = clean(el.textContent);
                if (ok(c)) return c;
            }
        }
        // Method 3: series/playlist anchor links
        for (const a of document.querySelectorAll('a[href*="/series/"], a[href*="playlist"]')) {
            const c = clean(a.textContent);
            if (ok(c)) return c;
        }
        // Method 4: class-name patterns for series titles
        for (const el of document.querySelectorAll('[class*="SeriesTitle"],[class*="PlaylistTitle"],[class*="SeriesName"]')) {
            const c = clean(el.textContent);
            if (ok(c)) return c;
        }
        // Method 5: "Episodes" sidebar header — look for headings in its container
        const allEls = Array.from(document.querySelectorAll('*'));
        const epsHeader = allEls.find(el => (el.textContent || '').trim() === 'Episodes' && el.children.length === 0);
        if (epsHeader) {
            let p = epsHeader.parentElement;
            for (let i = 0; i < 5 && p; i++) {
                for (const h of p.querySelectorAll('h1,h2,h3,h4,[class*="Title"]')) {
                    const c = clean(h.textContent);
                    if (ok(c) && c.toLowerCase() !== 'episodes') return c;
                }
                p = p.parentElement;
            }
        }
        // Method 6: "Drama ⭐ Watch all N episodes" button text (junk stripped)
        for (const el of allEls) {
            const low = (el.textContent || '').toLowerCase();
            if ((low.includes('drama') || low.includes('series')) && low.includes('episode')) {
                const c = clean(el.textContent)
                    .replace(/\\s*(?:Watch|View)\\s+all\\s+\\d*\\s*episodes?.*$/gi, '')
                    .replace(/\\s*\\d+\\s*episodes?\\s*$/gi, '')
                    .replace(/\\s*(?:EP|Episode|Ep)\\.?\\s*\\d+.*$/gi, '')
                    .replace(/^(?:Drama|Series|Playlist)\\s*[:—-]\\s*/i, '')
                    .trim();
                if (ok(c)) return c;
            }
        }
        // Method 7: video description first line
        const parts = document.querySelectorAll('[class*="DivVideoDescription"], [class*="VideoDescription"]');
        for (const p of parts) {
            const text = (p.textContent || '').trim();
            if (text) {
                const firstLine = text.split(/\\n/)[0].trim();
                if (firstLine) return firstLine;
            }
        }
        return null;
    }""")
    if title:
        return title

    # Fallback: clean the caption from rehydration data
    desc = await page.evaluate("""() => {
        const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (!s) return null;
        try {
            const d = JSON.parse(s.textContent);
            return d.__DEFAULT_SCOPE__?.['webapp.video-detail']?.itemInfo?.itemStruct?.desc || null;
        } catch (e) { return null; }
    }""")
    return _clean_series_title(desc)


async def _get_series_cover(page) -> str | None:
    """Extract the OFFICIAL series cover/poster URL (never a random
    episode's thumbnail).

    Priority:
      1. Rehydration data — cover/poster/banner fields on series/playlist
         objects (episode-level video covers are only used as a last resort)
      2. DOM — ``.series-cover`` / ``[data-e2e*="cover"]`` image elements
    Returns None when no official cover is found — the bot then uses
    Episode 1's cover only.
    """
    return await page.evaluate("""() => {
        const urlOk = (u) => typeof u === 'string' &&
            /^https?:\\/\\//i.test(u) && !/playAddr|\\.mp4|video\\//i.test(u) &&
            !/avatar|avatarLarger/.test(u || '');
        const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        const official = [];
        const episodic = [];
        if (s) {
            try {
                const d = JSON.parse(s.textContent);
                const scope = d.__DEFAULT_SCOPE__ || {};
                (function walk(obj, path) {
                    if (!obj || typeof obj !== 'object') return;
                    if (Array.isArray(obj)) {
                        for (let i = 0; i < obj.length; i++) walk(obj[i], path + '[' + i + ']');
                        return;
                    }
                    const pl = path.toLowerCase();
                    if (/series|playlist|collection|drama/.test(pl)) {
                        for (const k of Object.keys(obj)) {
                            const v = obj[k];
                            if (typeof v === 'string' && urlOk(v) &&
                                /cover|poster|banner|thumb|picture|image/i.test(k)) {
                                if (/video|episode|item/i.test(pl)) episodic.push(v);
                                else official.push(v);
                            }
                        }
                    }
                    for (const k of Object.keys(obj)) {
                        walk(obj[k], path ? path + '.' + k : k);
                    }
                })(scope, '');
                if (official.length) return official[0];
            } catch (e) {}
        }
        // DOM: official series-cover elements (side panel header image)
        const selectors = [
            '[class*="series-cover"] img', '[class*="SeriesCover"] img',
            '[class*="playlist-cover"] img', '[class*="PlaylistCover"] img',
            '[class*="SeriesCoverImg"] img', '[data-e2e*="series-cover"] img',
            '[data-e2e*="seriesCover"] img', '[data-e2e*="playlist-cover"] img',
            '[class*="series-cover"]', '[class*="SeriesCover"]',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (!el) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width < 50) continue;  // skip tiny avatar thumbnails
            let src = '';
            if (el.tagName === 'IMG') src = el.currentSrc || el.src || '';
            else {
                const img = el.querySelector('img');
                if (img) src = img.currentSrc || img.src || '';
                if (!src) {
                    const bg = getComputedStyle(el).backgroundImage || '';
                    const m = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
                    if (m) src = m[1];
                }
            }
            if (urlOk(src)) return src;
        }
        // Last resort: an episode-level cover from series data
        if (episodic.length) return episodic[0];
        return null;
    }""")


_EPISODE_MARKER_RE = re.compile(r"\s*(?:EP\.?|Episode|Ep|Part)\s*\d+.*$", re.IGNORECASE)


def _clean_series_title(text: str | None) -> str | None:
    """Clean a caption/text into a plausible series title.

    Strips emojis, "watch all N episodes" junk, leading "Drama" labels and
    trailing "Episode N" / "EP.N" markers.  Returns None when nothing usable
    remains (e.g. the text was only "Episode 1 EP").
    """
    if not text:
        return None
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)
    t = re.sub(r"\s*(?:Watch|View)\s+all\s+\d*\s*episodes?.*$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*\d+\s*episodes?\s*$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^\s*(?:Drama|Series|Playlist)\s*[:—-]\s*", "", t, flags=re.IGNORECASE).strip()
    t = _EPISODE_MARKER_RE.sub("", t).strip()
    t = re.sub(r"[.,:;—–\-|]+$", "", t).strip()
    t = re.sub(r"\s{2,}", " ", t).strip()
    low = t.lower()
    if not t or len(t) > 200 or low in ("drama", "series", "playlist", "episode", "ep"):
        return None
    if re.match(r"^(?:ep(?:isode)?\.?\s*\d*\s*)+$", low):
        return None
    return t


# ── Rehydration data scanner (Strategy A) ─────────────────────────────────


async def _deep_scan_rehydration(page) -> dict:
    """Scan ALL ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` data for series/playlist keys.
    
    Returns a dict with:
    - ``top_keys``: all top-level keys of ``__DEFAULT_SCOPE__``
    - ``series_fields``: any nested fields whose key contains series/playlist/episode/collection
    - ``series_data``: extracted series data (if found)
    - ``error``: error message if parsing failed
    """
    return await page.evaluate("""() => {
        const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (!s) return {error: 'Rehydration script tag not found on page'};
        try {
            const d = JSON.parse(s.textContent);
            const scope = d.__DEFAULT_SCOPE__ || {};
            const topKeys = Object.keys(scope);

            // Recursively find all keys matching series/playlist/episode
            const searchTerms = ['series', 'playlist', 'episode', 'collection', 'playlist'];
            const found = {};

            function deepSearch(obj, path) {
                if (!obj || typeof obj !== 'object') return;
                for (const key of Object.keys(obj)) {
                    const currentPath = path ? path + '.' + key : key;
                    const kl = key.toLowerCase();
                    if (searchTerms.some(t => kl.includes(t))) {
                        const val = obj[key];
                        if (val && typeof val === 'object') {
                            const keys = Array.isArray(val)
                                ? '[' + val.length + ' items]'
                                : Object.keys(val).slice(0, 20);
                            found[currentPath] = {
                                type: Array.isArray(val) ? 'array' : typeof val,
                                keys: keys,
                                hasId: !!(val.id || val.ID || val.seriesId),
                            };
                            if (val.videoList || val.videos || val.itemList || val.items) {
                                found[currentPath]._preview = JSON.stringify(val).slice(0, 2000);
                            }
                        } else {
                            found[currentPath] = { type: typeof val, value: String(val).slice(0, 200) };
                        }
                    }
                    if (typeof obj[key] === 'object' && obj[key] !== null) {
                        deepSearch(obj[key], currentPath);
                    }
                }
            }
            deepSearch(scope, '');

            // Also check within video-detail itemStruct
            const item = scope['webapp.video-detail']?.itemInfo?.itemStruct;
            if (item) {
                for (const k of Object.keys(item)) {
                    const kl = k.toLowerCase();
                    if (searchTerms.some(t => kl.includes(t))) {
                        found['itemStruct.' + k] = item[k];
                    }
                }
            }

            return { top_keys: topKeys, series_fields: found, key_count: Object.keys(found).length };
        } catch(e) {
            return {error: 'Parse error: ' + e.message};
        }
    }""")


async def _extract_series_from_rehydration(page, username: str) -> list[dict] | None:
    """Try to extract series episode URLs directly from rehydration data.
    
    Searches all nested keys for video lists embedded in series/playlist data.
    Returns a list of episode dicts matching the standard format, or None.
    """
    return await page.evaluate(f"""() => {{
        const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (!s) return null;
        try {{
            const d = JSON.parse(s.textContent);
            const scope = d.__DEFAULT_SCOPE__ || {{}};
            const episodes = [];
            const seenIds = new Set();
            const username = {json.dumps(username)};

            function extractVideos(obj, path) {{
                if (!obj || typeof obj !== 'object') return;
                // Check if this object has a list of videos
                for (const listKey of ['videoList', 'videos', 'itemList', 'items', 'episodeList', 'episodes']) {{
                    const list = obj[listKey];
                    if (Array.isArray(list) && list.length > 1) {{
                        for (const item of list) {{
                            let vid = item.id || item.video_id || item.ID || '';
                            if (!vid && item.video) vid = item.video.id || '';
                            if (!vid && item.url) {{
                                const m = item.url.match(/video\\/(\\d+)/);
                                if (m) vid = m[1];
                            }}
                            if (vid && !seenIds.has(vid)) {{
                                seenIds.add(vid);
                                episodes.push({{
                                    episode: episodes.length + 1,
                                    id: vid,
                                    url: `https://www.tiktok.com/${{username ? '@' + username : ''}}/video/${{vid}}`,
                                }});
                            }}
                        }}
                        if (episodes.length > 1) return;
                    }}
                }}
                // Recurse into child objects
                for (const key of Object.keys(obj)) {{
                    if (typeof obj[key] === 'object' && obj[key] !== null) {{
                        extractVideos(obj[key], path + '.' + key);
                    }}
                }}
            }}

            extractVideos(scope, '__DEFAULT_SCOPE__');

            // Also directly check video-detail's itemStruct for collection/series info
            const item = scope['webapp.video-detail']?.itemInfo?.itemStruct;
            if (item) {{
                const related = item.relatedItemList || item.relatedItems || [];
                if (related.length > 1) {{
                    // Related items from the same series
                    const username = (item.author?.uniqueId || username);
                    for (const r of related) {{
                        const vid = r.id || '';
                        if (vid && !seenIds.has(vid)) {{
                            seenIds.add(vid);
                            episodes.push({{
                                episode: episodes.length + 1,
                                id: vid,
                                url: `https://www.tiktok.com/${{username ? '@' + username : ''}}/video/${{vid}}`,
                            }});
                        }}
                    }}
                }}
            }}

            return episodes.length > 1 ? episodes.sort((a, b) => a.episode - b.episode) : null;
        }} catch(e) {{
            return null;
        }}
    }}""")


# ── DOM page analysis (Strategy B) ────────────────────────────────────────


async def _dump_page_structure(page, label: str = "") -> dict:
    """Dump key page elements and structure for debugging.
    
    If *label* is non-empty, writes structure to
    ``page_structure_<label>.txt`` and saves a screenshot.
    """
    info = await page.evaluate("""() => {
        const info = {
            url: window.location.href,
            title: document.title,
            bodyTextSample: (document.body?.innerText || '').substring(0, 3000),
            dataE2e: {},
            episodeButtons: [],
            seriesLinks: [],
            seriesText: [],
            allAnchors: [],
        };
        for (const el of document.querySelectorAll('[data-e2e]')) {
            const attr = el.getAttribute('data-e2e');
            info.dataE2e[attr] = (info.dataE2e[attr] || 0) + 1;
        }
        for (const a of document.querySelectorAll('a')) {
            const href = a.href || '';
            const text = (a.textContent || '').trim();
            if (href) info.allAnchors.push({ href: href.slice(0, 120), text: text.slice(0, 60) });
            if (/series|playlist|episode/i.test(href) || /series|playlist|episode/i.test(text)) {
                info.seriesLinks.push({ href: href.slice(0, 200), text: text.slice(0, 80) });
            }
        }
        for (const el of document.querySelectorAll('button, a, div, span')) {
            const t = (el.textContent || '').trim();
            const num = parseInt(t, 10);
            if (!isNaN(num) && num > 0 && num <= 200 && String(num) === t) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 && rect.left < window.innerWidth && rect.top < window.innerHeight) {
                    info.episodeButtons.push({
                        text: t, tag: el.tagName.toLowerCase(),
                        left: Math.round(rect.left), top: Math.round(rect.top),
                        w: Math.round(rect.width), h: Math.round(rect.height),
                        cls: (el.className || '').slice(0, 80),
                    });
                }
            }
        }
        const bodyText = (document.body?.innerText || '').toLowerCase();
        const lines = bodyText.split('\\n').filter(l => /series|episode|playlist|ep/i.test(l)).slice(0, 10);
        info.seriesText = lines.map(l => l.trim().slice(0, 200)).filter(Boolean);
        return info;
    }""")
    if label:
        try:
            path = f"page_structure_{label}.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(info, indent=2))
            log.info("Page structure saved to %s", path)
        except Exception as e:
            log.warning("Failed to save page structure: %s", e)
        try:
            screenshot_path = f"page_structure_{label}.png"
            await page.screenshot(path=screenshot_path, full_page=True)
            log.info("Screenshot saved to %s", screenshot_path)
        except Exception as e:
            log.warning("Screenshot failed: %s", e)
    return info


# ── View Series link detection (Strategy C) ───────────────────────────────


async def _find_and_click_view_series(page) -> str | None:
    """Find a ``View series`` / ``Series`` / ``Episodes`` link/button on the video page and click it.
    
    Returns the URL of the series page if navigation occurred, or None.
    """
    return await page.evaluate("""() => {
        // Look for links/buttons containing series-related text
        const keywords = ['view series', 'view all', 'see all', 'series', 'episodes', 'playlist',
                          'all episodes', 'show all', 'view playlist'];
        const all = document.querySelectorAll('a, button, span, div');
        for (const el of all) {
            const t = (el.textContent || '').trim().toLowerCase();
            if (keywords.some(k => t.includes(k))) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    const href = el.tagName === 'A' ? (el.href || '') : (el.closest('a')?.href || '');
                    if (href && (href.includes('/series/') || href.includes('/playlist/') || href.includes('/video/'))) {
                        return href;
                    }
                    el.click();
                    return 'CLICKED';
                }
            }
        }
        return null;
    }""")


async def _wait_for_navigation_to_series(page, old_url: str, timeout: float = 15.0) -> str | None:
    """Wait for page navigation to a series/playlist page."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = page.url
        if ('/series/' in current or '/playlist/' in current) and current != old_url:
            return current
        await asyncio.sleep(1)
    return None


async def _extract_video_ids_from_page(page, username: str) -> list[dict]:
    """Extract all video IDs from the current page via data-e2e attributes and anchor links.
    
    Works on profile pages, series pages, or any page with video links.
    Returns list of {episode, id, url} sorted by appearance.
    """
    return await page.evaluate(f"""() => {{
        const username = {json.dumps(username)};
        const seenIds = new Set();
        const episodes = [];
        let epNum = 0;

        // Method 1: data-e2e attributes (most stable)
        const postItems = document.querySelectorAll('[data-e2e="user-post-item"] a, [data-e2e="user-post-item"]');
        for (const el of postItems) {{
            let href = el.href || el.getAttribute('href') || '';
            const link = el.tagName === 'A' ? el : el.querySelector('a');
            if (link) href = link.href || link.getAttribute('href') || '';
            const m = href.match(/\\/video\\/(\\d+)/);
            if (m && !seenIds.has(m[1])) {{
                seenIds.add(m[1]);
                epNum++;
                episodes.push({{
                    episode: epNum,
                    id: m[1],
                    url: `https://www.tiktok.com/${{username ? '@' + username : ''}}/video/${{m[1]}}`,
                }});
            }}
        }}

        // Method 2: all anchor links pointing to video pages
        for (const a of document.querySelectorAll('a[href*="/video/"]')) {{
            const m = (a.href || '').match(/\\/video\\/(\\d+)/);
            if (m && !seenIds.has(m[1])) {{
                seenIds.add(m[1]);
                epNum++;
                episodes.push({{
                    episode: epNum,
                    id: m[1],
                    url: `https://www.tiktok.com/${{username ? '@' + username : ''}}/video/${{m[1]}}`,
                }});
            }}
        }}

        return episodes;
    }}""")



# Shared JS snippet: find the episode sidebar container (all detection methods)
_SIDEBAR_FIND_JS = """() => {
    const threshold = window.innerWidth * 0.55;
    const candidates = [];
    // Method A: data-e2e attributes
    for (const sel of ['series', 'playlist', 'episode', 'Episode']) {
        const el = document.querySelector('[data-e2e*="' + sel + '"]');
        if (el) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) candidates.push(el);
        }
    }
    // Method B: class name patterns
    for (const el of document.querySelectorAll(
        '[class*="Episode"],[class*="episode"],[class*="playlist"],[class*="series"],' +
        '[class*="DivEpisode"],[class*="DivGrid"]'
    )) {
        const rect = el.getBoundingClientRect();
        if (rect.left > threshold && rect.width > 0 && rect.height > 50) candidates.push(el);
    }
    // Method C: rightmost large container
    let bestArea = 0;
    for (const el of document.querySelectorAll('div')) {
        const rect = el.getBoundingClientRect();
        if (rect.left > threshold && rect.width > 100 && rect.height > 100) {
            const area = rect.width * rect.height;
            if (area > bestArea) {
                bestArea = area;
                candidates.push(el);
            }
        }
    }
    // Prefer the candidate with the most direct children (episode grid has 20+)
    if (!candidates.length) return null;
    candidates.sort((a, b) => b.children.length - a.children.length);
    return candidates[0];
}"""

# Preferred finder: class-pattern grid match FIRST (proven to find the correct
# 24-child episode grid in _wait_for_grid), falling back to _SIDEBAR_FIND_JS.
_GRID_FIND_JS = f"""() => {{
    const patterns = {json.dumps(['DivEpisodeGrid', 'EpisodeList', 'EpisodeSidebar',
                                  'DivPlaylistContainer', 'DivSeriesContainer',
                                  'DivEpisodeContainer', 'EpisodeItem'])};
    // After a video navigation TikTok re-renders the sidebar and keeps the
    // OLD grid in the DOM underneath the NEW one.  Always return the LAST
    // matching grid — the fresh, topmost instance.
    let found = null;
    for (const el of document.querySelectorAll('*')) {{
        const c = el.className || '';
        if (typeof c === 'string' && patterns.some(p => c.includes(p))) {{
            if (el.children.length > 0) found = el;
        }}
    }}
    if (found) return found;
    const findSidebar = {_SIDEBAR_FIND_JS};
    return findSidebar();
}}"""


async def _reload_episode_buttons(page) -> list:
    """Get episode button data from the right sidebar of a TikTok series page.

    Finds the sidebar container, then collects all direct children (episode
    items).  Returns them as sequentially-numbered buttons regardless of
    whether they have visible numeric text.

    Also looks for ``data-e2e`` attributes, class patterns, and anchor
    links as fallbacks.

    Returns list of {index, text, epNumber, videoId?, isActive}.
    """
    return await page.evaluate(r"""() => {
        // ── Step 1: Find the sidebar container (shared detection) ───────
        const threshold = window.innerWidth * 0.55;
        let sidebar = null;
        let sidebarClass = '';
        const candidates = [];

        // Method A: data-e2e attributes
        for (const sel of ['series', 'playlist', 'episode', 'Episode']) {
            const el = document.querySelector('[data-e2e*="' + sel + '"]');
            if (el) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) candidates.push(el);
            }
        }

        // Method B: class name patterns
        for (const el of document.querySelectorAll(
            '[class*="Episode"],[class*="episode"],[class*="playlist"],[class*="series"],' +
            '[class*="DivEpisode"],[class*="DivGrid"]'
        )) {
            const rect = el.getBoundingClientRect();
            if (rect.left > threshold && rect.width > 0 && rect.height > 50) candidates.push(el);
        }

        // Method C: rightmost large container
        let bestArea = 0;
        for (const el of document.querySelectorAll('div')) {
            const rect = el.getBoundingClientRect();
            if (rect.left > threshold && rect.width > 100 && rect.height > 100) {
                const area = rect.width * rect.height;
                if (area > bestArea) {
                    bestArea = area;
                    candidates.push(el);
                }
            }
        }

        // Prefer candidate with most children (episode grid has 20+)
        if (candidates.length) {
            candidates.sort((a, b) => b.children.length - a.children.length);
            sidebar = candidates[0];
            sidebarClass = (sidebar.className || '').slice(0, 80);
        }
        if (!sidebar) return [];

        // ── Step 2: Get direct children of the sidebar (= episode items) ─
        const children = Array.from(sidebar.children);
        const btns = [];

        for (let i = 0; i < children.length; i++) {
            const child = children[i];
            const txt = (child.textContent || '').trim().slice(0, 80);
            const c = child.className || '';
            const rect = child.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            // Extract video ID from nested anchor if present
            let videoId = null;
            const anchor = child.querySelector('a[href*="/video/"]');
            if (anchor) {
                const m = (anchor.href || '').match(/\/video\/(\d+)/);
                if (m) videoId = m[1];
            }

            // Determine episode number from text or position
            let epNumber = i + 1;
            // Try to extract number from text
            const bareNum = parseInt(txt);
            if (!isNaN(bareNum) && bareNum > 0 && bareNum <= 200) {
                epNumber = bareNum;
            } else {
                const epMatch = txt.match(/\b(?:EP|Ep|ep|Episode|episode|Part|part)[.\t ]*(\d+)\b/);
                if (epMatch) epNumber = parseInt(epMatch[1]);
            }

            btns.push({
                index: i,
                text: txt || ('EP ' + epNumber),
                epNumber: epNumber,
                isActive: c.includes('aktjs9') || child.getAttribute('aria-current') === 'true' || c.includes('active'),
                tag: child.tagName.toLowerCase(),
                videoId: videoId,
                sidebarClass: sidebarClass,
            });
        }

        return btns;
    }""")


async def _grid_marker(page) -> str | None:
    """JSON string of the first few episode-grid children's text.

    Used to detect when a tab click has switched the grid content.
    """
    return await page.evaluate(f"""() => {{
        const findGrid = {_GRID_FIND_JS};
        const sidebar = findGrid();
        if (!sidebar || !sidebar.children.length) return null;
        return JSON.stringify(Array.from(
            {{ length: Math.min(sidebar.children.length, 6) }},
            (_, i) => (sidebar.children[i].textContent || '').trim().slice(0, 24)
        ));
    }}""")


def _ep_from_text(text: str, tab_start: int | None = None, tab_end: int | None = None) -> int | None:
    """Extract an episode number from a sidebar item's text.

    Accepts bare numbers ("49") and "EP 49" / "Episode 49" styles.
    If *tab_start*/*tab_end* are given (from a "49-61" tab label), the
    number must fall inside (with a small tolerance) that range.
    Returns None when the text carries no usable episode number.
    """
    if not text:
        return None
    t = text.strip()
    m = re.match(r"^(\d{1,3})$", t)
    if not m:
        m = re.search(r"\b(?:EP|Ep|ep|Episode|episode|Part|part)[.\t ]*(\d{1,3})\b", t)
    if not m:
        return None
    num = int(m.group(1))
    if not (0 < num <= 500):
        return None
    if tab_start is not None and tab_end is not None:
        if not (tab_start - 2 <= num <= tab_end + 2):
            return None
    return num


async def _grid_child_count(page) -> int:
    """Number of VISIBLE episode-grid children currently in the DOM."""
    return await page.evaluate(f"""() => {{
        const findGrid = {_GRID_FIND_JS};
        const sidebar = findGrid();
        if (!sidebar) return 0;
        let n = 0;
        for (let i = 0; i < sidebar.children.length; i++) {{
            const r = sidebar.children[i].getBoundingClientRect();
            if (r.width > 0 && r.height > 0) n++;
        }}
        return n;
    }}""")


async def _wait_for_grid_stable(page, timeout: float = 20.0) -> int:
    """Wait until the tab's episode grid is FULLY rendered: the visible
    child count must stay the same across consecutive polls (and be > 0).

    Returns the final visible child count.
    """
    import time
    deadline = time.monotonic() + timeout
    prev_count = -1
    stable = 0
    count = 0
    while time.monotonic() < deadline:
        count = await _grid_child_count(page)
        if count == prev_count:
            stable += 1
            if stable >= 3 and count > 0:
                log.info("Grid render stable: %d children fully in DOM", count)
                return count
        else:
            stable = 0
        prev_count = count
        await asyncio.sleep(0.8)
    log.warning("Grid render did not stabilise within %.0fs (count=%d)", timeout, count)
    return count


async def _scroll_sidebar_full(page) -> None:
    """Scroll the episode sidebar top→bottom→top to force ALL of the tab's
    episodes to render in the DOM (guards against lazy rendering)."""
    el = await page.evaluate_handle(f"""() => {{
        const threshold = window.innerWidth * 0.55;
        let best = null;
        for (const el of document.querySelectorAll('div')) {{
            const rect = el.getBoundingClientRect();
            if (rect.left > threshold && rect.width > 100 && rect.height > 50
                && el.scrollHeight > el.clientHeight + 5) {{
                if (!best || el.scrollHeight - el.clientHeight > best.scrollHeight - best.clientHeight) {{
                    best = el;
                }}
            }}
        }}
        return best;
    }}""")
    try:
        for _ in range(2):
            for _ in range(12):
                moved = await page.evaluate("""(el) => {
                    if (!el) return 0;
                    const before = el.scrollTop;
                    el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + 700);
                    return el.scrollTop - before;
                }""", el)
                if not isinstance(moved, int) or moved <= 0:
                    break
                await page.wait_for_timeout(250)
            await page.evaluate("""(el) => { if (el) el.scrollTop = 0; }""", el)
            await page.wait_for_timeout(400)
    finally:
        await el.dispose()


async def _is_item_active(page, text: str) -> bool:
    """True when the sidebar button with the given number label is the
    currently-playing (highlighted/active) episode."""
    return await page.evaluate(f"""() => {{
        const findGrid = {_GRID_FIND_JS};
        const sidebar = findGrid();
        if (!sidebar) return false;
        const wanted = {json.dumps(text)};
        for (let i = 0; i < sidebar.children.length; i++) {{
            const child = sidebar.children[i];
            if ((child.textContent || '').trim() !== wanted) continue;
            const c = child.className || '';
            return child.getAttribute('aria-current') === 'true' || /active/i.test(c) || /aktjs9/.test(c);
        }}
        return false;
    }}""")


async def _wait_for_tab_content(page, prev_marker: str | None, timeout: float = 25.0) -> bool:
    """Wait for the episode grid DOM to switch to a new tab's content.

    *prev_marker* is a JSON string of the first few children's text captured
    BEFORE the tab click.  Waits for network idle, then polls until the
    grid's children text differs from *prev_marker* (meaning the new tab's
    episodes have loaded).
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        log.debug("networkidle not reached within 8s after tab click")
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        marker = await _grid_marker(page)
        if marker and marker != prev_marker:
            log.info("Tab content switched (marker changed)")
            await asyncio.sleep(1.0)
            return True
        await asyncio.sleep(0.8)
    log.warning("Tab content did not switch within %.0fs", timeout)
    return False


async def _click_tab_buttons(page) -> list:
    """Click pagination tab buttons in the episode sidebar (e.g. "1-24", "25-40").
    
    Deduplicates tabs by text — only the first visible element with a given
    tab label is used (avoids matching parent/child duplicates).
    
    Returns the combined list of episode button descriptors from all tabs.
    """
    all_buttons = []
    seen_ep_numbers = set()

    # Find tabs: deduplicate by text content (keep first visible match per text)
    tabs = await page.evaluate("""() => {
        const seenText = new Set();
        const tabs = [];
        // Only look at actual buttons and anchor tags (not div/spans that contain child buttons)
        const all = document.querySelectorAll('button, a');
        for (const el of all) {
            const t = (el.textContent || '').trim();
            // Tab pattern: "1-24", "25-40", etc.
            if (/^\\d+\\s*-\\s*\\d+$/.test(t) && !seenText.has(t)) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    seenText.add(t);
                    tabs.push({ text: t, index: tabs.length });
                }
            }
        }
        return tabs;
    }""")

    if not tabs:
        log.info("No pagination tabs found, trying scroll-based loading")
        # Try clicking "Show all" first
        clicked = await page.evaluate("""() => {
            const links = document.querySelectorAll('a, button, span, div');
            for (const el of links) {
                const t = (el.textContent || '').trim().toLowerCase();
                if ((t.includes('see all') || t.includes('show all') || t.includes('view all'))
                    && !t.includes('comments')) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            log.info("Clicked 'Show all' link")
            await asyncio.sleep(3)
        return await _collect_buttons_from_current_view(page, seen_ep_numbers)

    log.info("Found %d pagination tabs: %s", len(tabs), [t['text'] for t in tabs])

    for tab_index, tab in enumerate(tabs):
        # Snapshot current grid content marker BEFORE clicking the tab, so we
        # can wait for the DOM to switch to the new tab's episodes.
        prev_marker = await _grid_marker(page)

        # Click the tab
        clicked_tab = await page.evaluate(f"""() => {{
            const all = document.querySelectorAll('button, a');
            for (const el of all) {{
                const t = (el.textContent || '').trim();
                if (/^\\d+\\s*-\\s*\\d+$/.test(t) && t === {json.dumps(tab['text'])}) {{
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {{
                        el.click();
                        return true;
                    }}
                }}
            }}
            return false;
        }}""")
        if clicked_tab:
            log.info("Clicked tab '%s'", tab['text'])
            await page.wait_for_timeout(2000)
            await _random_sleep(1.0, 2.0)
            if tab_index > 0:
                # Wait for network + DOM to load the new tab's episodes
                await _wait_for_tab_content(page, prev_marker, timeout=15.0)
            else:
                # First tab is already active; content won't change, just wait briefly
                await asyncio.sleep(2)

        # Scroll sidebar incrementally to reveal lazily-rendered items
        sidebar_scrolled = await page.evaluate("""() => {
            const threshold = window.innerWidth * 0.55;
            const candidates = [];
            for (const el of document.querySelectorAll('div')) {
                const rect = el.getBoundingClientRect();
                if (rect.left > threshold && rect.width > 100 && rect.height > 50) {
                    if (el.scrollHeight > el.clientHeight + 5) {
                        candidates.push({ el, diff: el.scrollHeight - el.clientHeight });
                    }
                }
            }
            candidates.sort((a, b) => b.diff - a.diff);
            return candidates.length > 0 ? candidates[0].diff : 0;
        }""")

        if isinstance(sidebar_scrolled, int) and sidebar_scrolled > 0:
            # Scroll in steps to trigger lazy loading
            for scroll_step in range(5):
                await page.evaluate("""() => {
                    const threshold = window.innerWidth * 0.55;
                    const candidates = [];
                    for (const el of document.querySelectorAll('div')) {
                        const rect = el.getBoundingClientRect();
                        if (rect.left > threshold && rect.width > 100 && rect.height > 50) {
                            if (el.scrollHeight > el.clientHeight + 5) {
                                candidates.push({ el, diff: el.scrollHeight - el.clientHeight });
                            }
                        }
                    }
                    candidates.sort((a, b) => b.diff - a.diff);
                    if (candidates.length > 0) {
                        candidates[0].el.scrollTop += 600;
                    }
                }""")
                await asyncio.sleep(1.5)

        # Collect buttons after scrolling
        tab_buttons = await _collect_buttons_from_current_view(page, seen_ep_numbers)
        log.info("Tab '%s' yielded %d new buttons", tab['text'], len(tab_buttons))
        all_buttons.extend(tab_buttons)

        if not clicked_tab:
            log.warning("Failed to click tab '%s'", tab['text'])

    return all_buttons


async def _collect_buttons_from_current_view(page, seen_ep_numbers: set) -> list:
    """Collect episode buttons visible in the current view, skipping already-seen episode numbers."""
    buttons = []
    raw = await _reload_episode_buttons(page)
    for b in raw:
        ep_num = b["epNumber"]
        if ep_num > 0 and ep_num not in seen_ep_numbers:
            seen_ep_numbers.add(ep_num)
            buttons.append(b)
    return buttons


async def _reload_all_episodes(page, max_episodes: int = 70) -> list:
    """Load ALL episode buttons by clicking pagination tabs and scrolling.
    
    TikTok shows ~24 buttons per tab ("1-24", "25-40", etc.). This function
    clicks each tab, finds the scrollable sidebar container, scrolls it
    to reveal all buttons, and collects them.
    
    Returns the full list of button descriptors.
    """
    buttons = await _click_tab_buttons(page)

    if len(buttons) < max_episodes:
        seen = {b["epNumber"] for b in buttons}
        for scroll_attempt in range(8):
            scrolled = await page.evaluate("""() => {
                // Find the sidebar container — the right-side panel with scrollable content
                const threshold = window.innerWidth * 0.55;
                const candidates = [];
                for (const el of document.querySelectorAll('div')) {
                    const rect = el.getBoundingClientRect();
                    if (rect.left > threshold && rect.width > 100 && rect.height > 50) {
                        if (el.scrollHeight > el.clientHeight + 5) {
                            candidates.push({ el, scrollDiff: el.scrollHeight - el.clientHeight });
                        }
                    }
                }
                // Sort by scroll difference (largest = most scrollable = sidebar)
                candidates.sort((a, b) => b.scrollDiff - a.scrollDiff);
                if (candidates.length > 0) {
                    const target = candidates[0].el;
                    target.scrollTop = target.scrollHeight;
                    return true;
                }
                // Fallback: scroll the whole page
                window.scrollTo(0, document.body.scrollHeight);
                return false;
            }""")
            if scrolled:
                await asyncio.sleep(2)
                new_btns = await _collect_buttons_from_current_view(page, seen)
                if new_btns:
                    buttons.extend(new_btns)
                    log.info("Scroll %d: added %d more buttons (total %d)", scroll_attempt + 1, len(new_btns), len(buttons))
                else:
                    break
            else:
                break

    log.info("Loaded %d episode buttons total", len(buttons))
    return buttons


async def _wait_for_video_change(page, old_url: str, timeout: float = 20.0) -> str | None:
    """Wait for the page URL to change (video navigation) or the video ID in DOM to update.
    
    Returns the new video ID, or None if unchanged.
    Falls back to checking ``_get_current_video_id`` if the wait expires.
    """
    import time
    deadline = time.monotonic() + timeout
    old_id = re.search(r"/video/(\d+)", old_url)
    old_id = old_id.group(1) if old_id else None
    log.info("Waiting for video change, old URL=%s old_id=%s", old_url, old_id)

    while time.monotonic() < deadline:
        # Check URL change
        current_url = page.url
        m = re.search(r"/video/(\d+)", current_url)
        if m and m.group(1) != old_id:
            log.info("Detected video change via URL: %s", m.group(1))
            return m.group(1)

        await asyncio.sleep(0.5)

    # Fallback: try to get whatever video is currently loaded
    log.warning("Timed out waiting for video change (%.1fs) — trying fallback", timeout)
    fallback = await _get_current_video_id(page)
    if fallback and fallback != old_id:
        log.info("Fallback got new video ID: %s", fallback)
        return fallback

    log.warning("No video change detected after %.1fs. Current URL: %s", timeout, page.url)
    return None


async def _dismiss_login_overlay(page) -> bool:
    """Best-effort close of TikTok's "Log in to start watching" modal.

    This modal intermittently covers the episode sidebar after a video
    navigation; clicking its dismiss/close button (or any empty icon
    button) unblocks the grid.  Never raises.
    """
    try:
        return await page.evaluate("""() => {
            const divs = [...document.querySelectorAll('div')];
            for (const d of divs) {
                const t = (d.textContent || '').trim();
                if (t.length > 5 && t.length < 250 && t.includes('Log in to start watching')) {
                    const btns = [...d.querySelectorAll('button, [role="button"]')];
                    for (const b of btns) {
                        const bt = (b.textContent || '').trim().toLowerCase();
                        if (!bt || bt.includes('later') || bt.includes('close') || bt.includes('no')) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }
            }
            return false;
        }""")
    except Exception:
        return False


async def _click_episode_item(page, item: dict, ep_num: int, old_url: str, username: str) -> str | None:
    """Click a sidebar episode button by its exact number label.

    Uses a Playwright locator so the button is re-resolved at click time —
    TikTok re-renders/re-lays-out the grid after every navigation and
    index-based coordinates go stale (clicks miss or hit the wrong cell).

    Returns the new video ID, or None if no change was detected.
    The caller wraps this in ``asyncio.wait_for`` so a hung page JS thread
    cannot freeze the whole extraction.
    """
    text = (item.get("text") or "").strip()
    if not text:
        log.warning("EP %d: sidebar item has no label text", ep_num)
        return None
    loc = (page.locator('[class*="ButtonEpisode"]:visible')
              .filter(has_text=re.compile(f"^{re.escape(text)}$")).last)
    try:
        await loc.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        await loc.click(timeout=12000)
    except Exception as e:
        log.warning("EP %d: locator click failed (%s) — dismissing overlay and retrying", ep_num, str(e)[:80])
        await _dismiss_login_overlay(page)
        await _random_sleep(1.0, 2.0)
        try:
            await loc.click(timeout=12000)
        except Exception as e2:
            log.warning("EP %d: retry click failed too: %s", ep_num, str(e2)[:80])
            return None
    log.info("Clicked sidebar item %s (EP %d), waiting for video change...", text, ep_num)
    return await _wait_for_video_change(page, old_url, timeout=12.0)


def _net_is_up(host: str = "www.tiktok.com", timeout: float = 4.0) -> bool:
    """Quick TCP/DNS connectivity check — True if the host is reachable.

    Used to fail fast when the machine's network/DNS is down (a dead
    connection used to wedge recovery for ~20 minutes with silent retries).
    """
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


async def _recover_page(page, browser, url: str, on_response=None):
    """Close a hung page and open a fresh one back on the target URL.

    Callers MUST wrap this in ``asyncio.wait_for`` — a wedged browser must
    never block the extraction indefinitely.  When the network itself is
    down, fail fast instead of sleeping and retrying in silence.
    """
    if not await asyncio.to_thread(_net_is_up):
        raise ConnectionError("Network is down (DNS/TCP check failed) — aborting page recovery")
    try:
        await page.close()
    except Exception:
        pass
    new_page = await browser.new_page()
    try:
        new_page.set_default_timeout(15000)
    except Exception:
        pass
    if on_response is not None:
        try:
            new_page.on('response', on_response)
        except Exception:
            pass
    try:
        await new_page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        if "RESOLVED" in str(e) or "getaddrinfo" in str(e) or "Name or service not known" in str(e):
            raise ConnectionError(f"Network is down during re-navigation: {e}")
        log.warning("Recovered page: re-navigation failed (%s) — continuing with loaded page", e)
    await asyncio.sleep(3)
    return new_page


async def extract_episodes(
    url: str,
    *,
    headless: bool = True,
    save_on_success: bool = True,
    progress_cb=None,
) -> list[dict]:
    """Open a TikTok video/series URL and extract all episode URLs.

    Extraction strategies (tried in order):
      A. **Rehydration data** — scan the embedded JSON for series/playlist video lists
      B. **View Series link** — find and click a "View series" link, then extract videos
      C. **Sidebar buttons** — click numbered episode buttons in the right sidebar
      D. **Single video** fallback

    Returns a list of ``{"episode": int, "id": str, "url": str}`` sorted by
    episode number, or a single-entry list for non-series videos.

    Parameters
    ----------
    url : str
        Full TikTok video URL.
    headless : bool
        Run Playwright in headless mode (default True).
    save_on_success : bool
        Save storage state after successful extraction (default True).
    progress_cb : callable | None
        Called with a human-readable progress string after each episode is
        processed (and at major milestones).  Must be safe to call from any
        thread; exceptions are swallowed.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser, context = await create_playwright_context(p, headless=headless)
        page = await context.new_page()
        # Every Playwright wait (selector, evaluate, goto-without-timeout…)
        # is hard-bounded to 15s — a wedged browser main thread can never
        # freeze the extraction beyond this.  NOTE: set_default_timeout is a
        # SYNCHRONOUS method in the async API — awaiting it raises TypeError.
        page.set_default_timeout(15000)

        episodes = []
        buttons = []
        series_title = None
        series_cover = None
        username = ""
        page_video_id = None
        last_page_video_id = None
        captured_cdn_urls: list[str] = []
        debug_mode = '--debug' in sys.argv

        try:
            # ── Warm up session: navigate to TikTok main page first ────────────
            log.info("Warming up TikTok session on main page...")
            await page.goto("https://www.tiktok.com", timeout=30000, wait_until="domcontentloaded")
            await _random_sleep(2.0, 4.0)

            # Save storage after warm-up (refreshes cookies/CSRF tokens)
            await save_storage(context)
            log.info("Session saved after warm-up")

            # ── Network interceptor: capture CDN video URLs ─────────────────
            async def on_response(response):
                url = response.url
                if ('tiktokcdn.com' in url or '.mp4' in url or 'video' in url) and not url.endswith('.js'):
                    ctype = response.headers.get("content-type", "")
                    if 'video' in ctype or 'octet' in ctype or 'mp4' in ctype:
                        if url not in captured_cdn_urls:
                            captured_cdn_urls.append(url)
                            log.info("Captured CDN video URL: %.80s", url)

            page.on('response', on_response)

            log.info("Navigating to target URL: %s", url)
            # Use domcontentloaded instead of networkidle — TikTok keeps long-poll connections open
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("Navigation timeout (60s) for %s: %s — continuing with loaded page", url, e)
            await _random_sleep(4.0, 8.0)

            # Check for login wall — CAPTCHA / sign-in overlays
            async def _check_login_wall():
                return await page.evaluate("""() => {
                    const text = document.body?.innerText || '';
                    if (text.includes('Drag the slider to fit the puzzle')) return true;
                    if (text.includes('Log in to start watching')) return true;
                    if (text.includes('Sign in') && text.includes('TikTok')) return true;
                    // Check for captcha iframe
                    if (document.querySelector('iframe[src*="captcha"], iframe[src*="challenge"]')) return true;
                    // Check for login modal overlay
                    const modal = document.querySelector('[class*="Login"], [class*="login"], [data-e2e*="login"], [class*="SignModal"]');
                    if (modal) {
                        const rect = modal.getBoundingClientRect();
                        if (rect.width > 100 && rect.height > 100) return true;
                    }
                    // Empty page with puzzle token in text
                    if (text.length < 200 && /[A-F0-9]{32}/.test(text)) return true;
                    return false;
                }""")

            login_wall = await _check_login_wall()
            if login_wall:
                body_preview = (await page.evaluate('document.body?.innerText?.slice(0, 500)') or '').strip()
                log.warning("Login/CAPTCHA wall detected — body preview: %.200s", body_preview)
                await _dump_page_structure(page, "login-wall")
                log.warning("Session is rate-limited. Run: python scripts/login_tiktok.py")
                for attempt in range(3):
                    await page.goto("https://www.tiktok.com", timeout=30000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(5000)
                    try:
                        await page.wait_for_function(
                            "() => { const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__'); return s && s.textContent.length > 100; }",
                            timeout=15000
                        )
                        log.info("Session detected on TikTok main page (rehydration data present)")
                    except Exception:
                        log.warning("No rehydration data on main page (attempt %d/3)", attempt + 1)

                    try:
                        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    except Exception as e:
                        log.warning("Navigation timeout on retry %d: %s", attempt + 1, e)
                    await page.wait_for_timeout(5000)
                    login_wall = await _check_login_wall()
                    if not login_wall:
                        log.info("Login wall bypassed after refresh attempt %d", attempt + 1)
                        await save_storage(context)
                        break
                    log.warning("Login wall persists (attempt %d/3)", attempt + 1)
                    if attempt < 2:
                        await asyncio.sleep(5)
                else:
                    log.warning("Login wall persists after all refresh attempts — continuing anyway")

            # Wait for rehydration data to appear (indicates JavaScript hydration)
            await page.wait_for_timeout(3000)
            try:
                await page.wait_for_function(
                    "() => { const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__'); return s && s.textContent.length > 50; }",
                    timeout=15000
                )
                log.info("Rehydration data present on video page")
            except Exception:
                log.warning("Rehydration data not found on video page — page may not be fully loaded")

            # Get current video info.  Its REAL episode number is unknown at
            # this point (the user may have sent a mid-series URL) — it is
            # learned later when the sidebar item for it is clicked
            # (already-active detection), or used as the EP 1 fallback.
            current_id = await _get_current_video_id(page)
            username = re.search(r"@([\w.-]+)", url)
            username = username.group(1) if username else ""
            page_video_id = current_id
            if current_id:
                log.info("Currently-open video: %s (real episode learned via sidebar)", current_id)

            # Read the OFFICIAL series cover/poster FIRST (before any tab
            # clicking may replace the DOM) — never a random episode's
            # thumbnail.
            series_cover = await _get_series_cover(page)
            if series_cover:
                log.info("Official series cover: %.100s", series_cover)

            # ── STRATEGY A: Extract series from rehydration data ──────────────
            log.info("Strategy A: scanning rehydration data for series info...")
            rehydration_episodes = await _extract_series_from_rehydration(page, username)
            if rehydration_episodes and len(rehydration_episodes) > 1:
                log.info("Strategy A SUCCESS: found %d episodes via rehydration data", len(rehydration_episodes))
                # Merge with current video
                seen_ids = {e["id"] for e in episodes}
                for ep in rehydration_episodes:
                    if ep["id"] not in seen_ids:
                        seen_ids.add(ep["id"])
                        episodes.append(ep)
                if len(episodes) > 1:
                    series_title = await _get_series_title(page)
                    episodes.sort(key=lambda x: x["episode"])
                    if series_title:
                        episodes.insert(0, {"_meta": {"series_title": series_title, "series_cover": series_cover}})
                    log.info("Returning %d episodes from rehydration data", len(episodes))
                    if save_on_success:
                        await save_storage(context)
                    await browser.close()
                    return episodes

            # Random delay before trying next strategy
            await _random_sleep(2.0, 5.0)

            # ── STRATEGY B: Find and click "View Series" link ────────────────
            log.info("Strategy B: looking for View Series link on page...")
            series_url = await _find_and_click_view_series(page)
            if series_url:
                if series_url == 'CLICKED':
                    log.info("Clicked a series link — waiting for navigation...")
                    new_url = await _wait_for_navigation_to_series(page, url, timeout=15.0)
                    if new_url:
                        log.info("Navigated to series page: %s", new_url)
                        await page.wait_for_timeout(4000)
                    else:
                        log.info("No navigation detected after click — staying on current page")
                else:
                    log.info("Found series URL: %s — navigating...", series_url[:120])
                    try:
                        await page.goto(series_url, timeout=30000, wait_until="networkidle")
                        await page.wait_for_timeout(4000)
                    except Exception as e:
                        log.warning("Failed to navigate to series URL: %s", e)

                # Extract video links from the current (series) page
                series_eps = await _extract_video_ids_from_page(page, username)
                if series_eps and len(series_eps) > 1:
                    log.info("Strategy B SUCCESS: found %d episodes from series page", len(series_eps))
                    seen_ids = {e["id"] for e in episodes}
                    for ep in series_eps:
                        if ep["id"] not in seen_ids:
                            seen_ids.add(ep["id"])
                            episodes.append(ep)
                    if len(episodes) > 1:
                        series_title = await _get_series_title(page)
                        episodes.sort(key=lambda x: x["episode"])
                        # Re-number episodes sequentially
                        for i, ep in enumerate(episodes, 1):
                            ep["episode"] = i
                        if series_title:
                            episodes.insert(0, {"_meta": {"series_title": series_title, "series_cover": series_cover}})
                        if save_on_success:
                            await save_storage(context)
                        await browser.close()
                        return episodes

            # ── Debug mode: dump page structure ─────────────────────────────
            if debug_mode:
                log.info("=== DEBUG: dumping page structure ===")
                rehyd_keys = await _deep_scan_rehydration(page)
                log.info("Rehydration keys: %s", json.dumps(rehyd_keys, indent=2)[:5000])
                structure = await _dump_page_structure(page)
                log.info("Page structure: %s", json.dumps(structure, indent=2)[:5000])
                # Save screenshot
                try:
                    import time
                    screenshot_path = f"debug_screenshot_{int(time.time())}.png"
                    await page.screenshot(path=screenshot_path, full_page=True)
                    log.info("Screenshot saved to %s", screenshot_path)
                except Exception as e:
                    log.warning("Screenshot failed: %s", e)

            # Random delay before trying sidebar strategy
            await _random_sleep(2.0, 5.0)

            # ── STRATEGY C: Sidebar episode button clicking ─────────────────
            log.info("Strategy C: trying sidebar episode button extraction...")
            series_title = await _get_series_title(page)
            log.info("Series title from page: %s", series_title)

            # Wait for episode grid
            has_grid = await _wait_for_grid(page, timeout=20)

            if has_grid:
                buttons = await _reload_all_episodes(page, max_episodes=70)
            else:
                log.info("Grid container not found, trying direct sidebar button scan...")
                buttons = await _reload_episode_buttons(page)
                if len(buttons) > 1:
                    log.info("Fallback: found %d episode buttons via sidebar scan (no grid)", len(buttons))
                    clicked = await page.evaluate("""() => {
                        const links = document.querySelectorAll('a, button, span, div');
                        for (const el of links) {
                            const t = (el.textContent || '').trim().toLowerCase();
                            if ((t.includes('see all') || t.includes('show all') || t.includes('view all'))
                                && !t.includes('comments')) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }""")
                    if clicked:
                        log.info("Clicked 'Show all' link in fallback mode")
                        await asyncio.sleep(3)
                        buttons = await _reload_episode_buttons(page)
                else:
                    log.info("Fallback scan found no additional buttons (%d) — single video", len(buttons))
                    if not episodes and page_video_id:
                        episodes.append({
                            "episode": 1,
                            "id": page_video_id,
                            "url": f"https://www.tiktok.com/@{username}/video/{page_video_id}",
                        })
                    return episodes

            if len(buttons) <= 1 and has_grid:
                log.info("Grid found but no episode buttons loaded — single video")
                if not episodes and page_video_id:
                    episodes.append({
                        "episode": 1,
                        "id": page_video_id,
                        "url": f"https://www.tiktok.com/@{username}/video/{page_video_id}",
                    })
                return episodes

            log.info("Loaded %d episode buttons total", len(buttons))

            # ── Click visible sidebar items to extract video IDs ─────────────
            # Sidebar items are React divs with no anchor tags or data attributes.
            # The only way to get video IDs is clicking each visible one and
            # capturing the URL change.  We process one tab page at a time.
            seen_ids = set()

            # Collect the tab texts in correct order (already in _click_tab_buttons)
            tab_texts = await page.evaluate("""() => {
                const seenText = new Set();
                const tabs = [];
                for (const el of document.querySelectorAll('button, a')) {
                    const t = (el.textContent || '').trim();
                    if (/^[0-9]+[ \t]*-[ \t]*[0-9]+$/.test(t) && !seenText.has(t)) {
                        seenText.add(t);
                        tabs.push(t);
                    }
                }
                return tabs;
            }""")

            # Track which tabs we've already processed
            processed_tabs = set()

            # Expected last episode = end of the final tab (e.g. "49-61" → 61),
            # used for the gap-filling pass.
            def _tab_range(tab_key: str) -> tuple:
                m = re.match(r"^(\d+)\s*-\s*(\d+)$", tab_key)
                return (int(m.group(1)), int(m.group(2))) if m else (None, None)

            # Expected total = end of the final tab (e.g. "49-60" → 60),
            # used for completeness checks and the per-episode counter.
            expected_total = 0
            if isinstance(tab_texts, list) and tab_texts:
                _, last_end = _tab_range(str(tab_texts[-1]))
                expected_total = last_end or 0

            async def _process_tab(tab_key: str) -> int:
                """Click one pagination tab, wait until ALL of its episodes are
                fully rendered in the DOM, then click every episode item in
                order and collect the video IDs.  Returns the number of new
                episodes."""
                nonlocal page
                added = 0
                start, end = _tab_range(tab_key)
                log.info("Processing tab %s for direct clicking...", tab_key)

                # Click the tab (retry until the grid content actually switches)
                switched = len(tab_texts) <= 1  # single tab: already active
                prev_marker = await _grid_marker(page)
                for attempt in range(3):
                    if len(tab_texts) > 1:
                        clicked_tab = await page.evaluate(f"""() => {{
                            for (const el of document.querySelectorAll('button, a')) {{
                                const t = (el.textContent || '').trim();
                                if (t === {json.dumps(tab_key)}) {{
                                    const r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {{
                                        el.click();
                                        return true;
                                    }}
                                }}
                            }}
                            return false;
                        }}""")
                        if not clicked_tab:
                            log.warning("Tab %s not found for clicking", tab_key)
                            return 0
                        log.info("Clicked tab %s (attempt %d)", tab_key, attempt + 1)
                        # REQUIRED: give React time to swap the grid content
                        # (fast tab switch never skips episodes).
                        await page.wait_for_timeout(2000)
                        # Wait for network + DOM to load the new tab's episodes
                        if await _wait_for_tab_content(page, prev_marker, timeout=25.0):
                            switched = True
                            break
                        if tab_key == str(tab_texts[0]):
                            # First tab is usually ALREADY active — the marker
                            # never changes.  Grid content present = switched.
                            cnt = await _grid_child_count(page)
                            if cnt > 0:
                                log.info("Tab %s already active (%d children) — proceeding", tab_key, cnt)
                                switched = True
                                break
                        log.warning("Tab %s content did not switch (attempt %d) — retrying",
                                    tab_key, attempt + 1)
                        await _random_sleep(3.0, 5.0)
                        prev_marker = await _grid_marker(page)
                    else:
                        await page.wait_for_timeout(2000)
                        switched = True
                        break
                if not switched:
                    # Last resort: the grid may have switched even though the
                    # marker check failed — proceed if the grid has content.
                    cnt = await _grid_child_count(page)
                    if cnt > 0:
                        log.warning("Tab %s marker never changed, but grid has %d children — proceeding",
                                    tab_key, cnt)
                        switched = True
                    else:
                        log.error("Tab %s never switched content after 3 attempts", tab_key)
                        return 0

                # Wait until the episode grid container and its children are
                # actually present in the DOM — videos load lazily, and
                # reading the list too early is what used to skip episodes.
                try:
                    await page.wait_for_selector(
                        '[class*="DivEpisodeGrid"],[class*="EpisodeList"],'
                        '[class*="EpisodeSidebar"],[data-e2e*="episode"],'
                        '[data-e2e*="series"]',
                        timeout=15000,
                    )
                except Exception:
                    pass
                # Wait until the tab's episodes are FULLY rendered in the DOM
                # (stable child count), then force-render via sidebar scroll
                # and give the last items a moment to attach.
                await _wait_for_grid_stable(page, timeout=20.0)
                await _scroll_sidebar_full(page)
                await _random_sleep(1.0, 2.0)

                # Get sidebar children for the CURRENT tab (grid-class detection preferred)
                children_info = await page.evaluate(f"""() => {{
                    const findGrid = {_GRID_FIND_JS};
                    const sidebar = findGrid();
                    if (!sidebar) return [];
                    const items = [];
                    for (let i = 0; i < sidebar.children.length; i++) {{
                        const child = sidebar.children[i];
                        const r = child.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {{
                            items.push({{
                                index: i,
                                x: Math.round(r.left + r.width / 2),
                                y: Math.round(r.top + r.height / 2),
                                text: (child.textContent || '').trim().slice(0, 40),
                            }});
                        }}
                    }}
                    return items;
                }}""")

                log.info("Tab %s: %d sidebar children", tab_key, len(children_info))
                if not children_info:
                    log.warning("Tab %s: no sidebar children found", tab_key)
                    return 0

                existing = {e["episode"] for e in episodes if "_meta" not in e}
                for idx, item in enumerate(children_info):
                    # Episode number from the item's own text ("49" → EP 49),
                    # falling back to the tab offset (start + index).
                    ep_num = _ep_from_text(item["text"], start, end)
                    if ep_num is None:
                        ep_num = (start + idx) if start is not None else len(episodes) + 1
                    if ep_num in existing:
                        continue

                    old_url = page.url
                    try:
                        new_id = await asyncio.wait_for(
                            _click_episode_item(page, item, ep_num, old_url, username),
                            timeout=EPISODE_PROCESS_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        log.warning("EP %d: processing timed out after %.0fs — recovering browser "
                                    "page, aborting tab (gap-fill will revisit)",
                                    ep_num, EPISODE_PROCESS_TIMEOUT)
                        try:
                            page = await asyncio.wait_for(
                                _recover_page(page, browser, url, on_response),
                                timeout=30.0,
                            )
                        except Exception as rec_e:
                            log.error("Page recovery failed: %s", rec_e)
                        return 0
                    except Exception as e:
                        log.warning("Failed to click sidebar item %d: %s", ep_num, e)
                        new_id = None

                    if not new_id:
                        # No video change: the page may ALREADY be showing this
                        # episode (e.g. the user sent a mid-series URL).  Verify
                        # with the sidebar's active state, then retry once.
                        await page.wait_for_timeout(600)
                        if await _is_item_active(page, item["text"]):
                            cur = await _get_current_video_id(page)
                            if cur and cur not in seen_ids:
                                seen_ids.add(cur)
                                episodes.append({
                                    "episode": ep_num,
                                    "id": cur,
                                    "url": f"https://www.tiktok.com/@{username}/video/{cur}",
                                })
                                existing.add(ep_num)
                                added += 1
                                print(f"Episode {ep_num}/{expected_total} found -> {cur}", flush=True)
                                log.info("EP %d → %s (already showing, active)", ep_num, cur)
                                if progress_cb:
                                    try:
                                        progress_cb(f"⏳ Extracting episodes… EP {ep_num}/{end or '?'} "
                                                    f"(tab {tab_key}, found {len(episodes)})")
                                    except Exception:
                                        pass
                                await _random_sleep(1.0, 3.0)
                                continue
                        else:
                            log.warning("EP %d: click missed — retrying once", ep_num)
                            try:
                                new_id = await asyncio.wait_for(
                                    _click_episode_item(page, item, ep_num, page.url, username),
                                    timeout=EPISODE_PROCESS_TIMEOUT,
                                )
                            except asyncio.TimeoutError:
                                new_id = None

                    if new_id and new_id not in seen_ids:
                        seen_ids.add(new_id)
                        episodes.append({
                            "episode": ep_num,
                            "id": new_id,
                            "url": f"https://www.tiktok.com/@{username}/video/{new_id}",
                        })
                        existing.add(ep_num)
                        added += 1
                        print(f"Episode {ep_num}/{expected_total} found -> {new_id}", flush=True)
                        log.info("EP %d → %s", ep_num, new_id)
                    elif new_id and new_id in seen_ids:
                        log.info("EP %d duplicate (already have vid %s)", ep_num, new_id)
                    else:
                        log.warning("EP %d: no video change detected", ep_num)

                    if progress_cb:
                        try:
                            progress_cb(f"⏳ Extracting episodes… EP {ep_num}/{end or '?'} "
                                        f"(tab {tab_key}, found {len(episodes)})")
                        except Exception:
                            pass

                    await _random_sleep(1.0, 3.0)
                return added

            # Main pass: process every tab in order
            for tab_text in tab_texts if isinstance(tab_texts, list) else []:
                tab_key = str(tab_text)
                if tab_key in processed_tabs:
                    continue
                processed_tabs.add(tab_key)
                if progress_cb:
                    try:
                        progress_cb(f"⏳ Extracting episodes… tab {tab_key} of {len(tab_texts)}")
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(_process_tab(tab_key), timeout=TAB_PROCESS_TIMEOUT)
                except asyncio.TimeoutError:
                    log.error("Tab %s processing timed out (>%.0fs) — recovering page",
                              tab_key, TAB_PROCESS_TIMEOUT)
                    try:
                        page = await asyncio.wait_for(
                            _recover_page(page, browser, url, on_response),
                            timeout=30.0,
                        )
                    except Exception as rec_e:
                        log.error("Page recovery failed: %s", rec_e)

            # ── STRICT completeness: no episode may be skipped.  Every tab
            #    whose range still has missing numbers is revisited (up to
            #    2 rounds) before extraction is considered complete.
            if isinstance(tab_texts, list) and len(tab_texts) > 1:
                for round_no in range(2):
                    missing_tabs = []
                    for tab_text in tab_texts:
                        tab_key = str(tab_text)
                        start, end = _tab_range(tab_key)
                        if start is None:
                            continue
                        have = {e["episode"] for e in episodes if "_meta" not in e}
                        missing = [n for n in range(start, end + 1) if n not in have]
                        if missing:
                            missing_tabs.append((tab_key, missing))
                    if not missing_tabs:
                        break
                    for tab_key, missing in missing_tabs:
                        log.info("Gap-fill round %d for %s: missing episodes %s",
                                 round_no + 1, tab_key, missing)
                        if progress_cb:
                            try:
                                progress_cb(f"⏳ Gap-fill: {len(missing)} missing in tab {tab_key}…")
                            except Exception:
                                pass
                        try:
                            await asyncio.wait_for(_process_tab(tab_key), timeout=TAB_PROCESS_TIMEOUT)
                        except asyncio.TimeoutError:
                            log.error("Gap-fill %s timed out (>%.0fs) — recovering page",
                                      tab_key, TAB_PROCESS_TIMEOUT)
                            try:
                                page = await asyncio.wait_for(
                                    _recover_page(page, browser, url, on_response),
                                    timeout=30.0,
                                )
                            except Exception as rec_e:
                                log.error("Page recovery failed: %s", rec_e)

            # ── Final-episode guarantee ─────────────────────────────────────
            # The very last episode of the LAST tab is the most commonly
            # skipped one (tab timeout / lazy render / missed click).  Make
            # sure it is read: switch to the last tab, scroll the sidebar to
            # the bottom, click the LAST sidebar item; if that fails, capture
            # whatever video the page is currently showing as the missing
            # episode.
            async def _ensure_last_episode(tab_key: str, expected_ep: int) -> bool:
                nonlocal page
                start, end = _tab_range(tab_key)
                prev_marker = await _grid_marker(page)
                if len(tab_texts) > 1:
                    clicked = await page.evaluate(f"""() => {{
                        for (const el of document.querySelectorAll('button, a')) {{
                            const t = (el.textContent || '').trim();
                            if (t === {json.dumps(tab_key)}) {{
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {{
                                    el.click();
                                    return true;
                                }}
                            }}
                        }}
                        return false;
                    }}""")
                    if clicked:
                        log.info("Final pass: clicked last tab %s", tab_key)
                        await page.wait_for_timeout(2000)
                        await _random_sleep(1.0, 2.0)
                        await _wait_for_tab_content(page, prev_marker, timeout=25.0)
                else:
                    log.info("Final pass: single tab %s — no tab switch needed", tab_key)

                # Scroll the sidebar to the very bottom (items may lazy-render)
                for _ in range(6):
                    await page.evaluate("""() => {
                        const threshold = window.innerWidth * 0.55;
                        let best = null;
                        for (const el of document.querySelectorAll('div')) {
                            const rect = el.getBoundingClientRect();
                            if (rect.left > threshold && rect.width > 100 && rect.height > 50) {
                                if (el.scrollHeight > el.clientHeight + 5) {
                                    if (!best || el.scrollHeight - el.clientHeight > best.scrollHeight - best.clientHeight) {
                                        best = el;
                                    }
                                }
                            }
                        }
                        if (best) best.scrollTop = best.scrollHeight;
                        return !!best;
                    }""")
                    await asyncio.sleep(0.8)

                children_info = await page.evaluate(f"""() => {{
                    const findGrid = {_GRID_FIND_JS};
                    const sidebar = findGrid();
                    if (!sidebar) return [];
                    const items = [];
                    for (let i = 0; i < sidebar.children.length; i++) {{
                        const child = sidebar.children[i];
                        const r = child.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {{
                            items.push({{
                                index: i,
                                x: Math.round(r.left + r.width / 2),
                                y: Math.round(r.top + r.height / 2),
                                text: (child.textContent || '').trim().slice(0, 40),
                            }});
                        }}
                    }}
                    return items;
                }}""")
                if not children_info:
                    log.warning("Final pass: no sidebar items visible on %s", tab_key)
                    return False

                item = children_info[-1]
                item_ep = _ep_from_text(item["text"], start, end)
                target_ep = item_ep if item_ep is not None else expected_ep
                log.info("Final pass: clicking last sidebar item (idx %d, text %r) → EP %s",
                         item["index"], item["text"], target_ep)
                old_url = page.url
                try:
                    new_id = await asyncio.wait_for(
                        _click_episode_item(page, item, target_ep, old_url, username),
                        timeout=EPISODE_PROCESS_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    log.warning("Final pass: click on EP %s timed out", target_ep)
                    new_id = None

                if new_id and new_id not in seen_ids:
                    seen_ids.add(new_id)
                    episodes.append({
                        "episode": target_ep,
                        "id": new_id,
                        "url": f"https://www.tiktok.com/@{username}/video/{new_id}",
                    })
                    print(f"Episode {target_ep}/{expected_total} found -> {new_id}", flush=True)
                    log.info("Final pass: captured EP %d → %s", target_ep, new_id)
                    return True

                # Absolute fallback: the video now open on the page.
                cur_id = await _get_current_video_id(page)
                if cur_id and cur_id not in seen_ids:
                    seen_ids.add(cur_id)
                    episodes.append({
                        "episode": expected_ep,
                        "id": cur_id,
                        "url": f"https://www.tiktok.com/@{username}/video/{cur_id}",
                    })
                    print(f"Episode {expected_ep}/{expected_total} found -> {cur_id}", flush=True)
                    log.info("Final pass: captured currently-open video as EP %d → %s", expected_ep, cur_id)
                    return True
                log.warning("Final pass: EP %d still missing (page shows %s)", expected_ep, cur_id)
                return False

            if isinstance(tab_texts, list) and tab_texts and len(episodes) > 1:
                last_tab = str(tab_texts[-1])
                start, end = _tab_range(last_tab)
                have = {e["episode"] for e in episodes if "_meta" not in e}
                if start is not None and end is not None and end > 0 and end not in have:
                    log.info("Final-episode guarantee: EP %d missing — targeting last tab %s", end, last_tab)
                    try:
                        await asyncio.wait_for(
                            _ensure_last_episode(last_tab, end),
                            timeout=TAB_PROCESS_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        log.error("Final-episode pass timed out (>%.0fs)", TAB_PROCESS_TIMEOUT)
                        try:
                            page = await asyncio.wait_for(
                                _recover_page(page, browser, url, on_response),
                                timeout=30.0,
                            )
                        except Exception as rec_e:
                            log.error("Page recovery failed: %s", rec_e)

            # Snapshot the currently-open video — the bot uses it as a
            # last-resort fill source if the final episode still goes missing.
            try:
                last_page_video_id = await _get_current_video_id(page)
            except Exception:
                last_page_video_id = None

            if save_on_success and len(episodes) > 1:
                await save_storage(context)

        except Exception as e:
            log.error("Episode extraction failed: %s", e)
        finally:
            await browser.close()

        # Single-video fallback: nothing was extracted from a sidebar, so the
        # currently-open video itself is EP 1.
        if not episodes and page_video_id:
            episodes.append({
                "episode": 1,
                "id": page_video_id,
                "url": f"https://www.tiktok.com/@{username}/video/{page_video_id}",
            })

        episodes.sort(key=lambda x: x["episode"])
        expected = max(
            (int(m.group(2)) for t in (tab_texts if isinstance(tab_texts, list) else [])
             if (m := re.match(r"^(\d+)\s*-\s*(\d+)$", str(t)))),
            default=max(len(buttons), len(episodes)),
        )
        total_expected = max(expected, len(episodes))
        log.info("Extracted %d / %d episodes", len(episodes), total_expected)
        print(f"Extraction complete: {len(episodes)}/{total_expected} episodes collected", flush=True)

        if series_title and len(episodes) > 1:
            meta = {"series_title": series_title}
            # The OFFICIAL series cover/poster — the bot uses it as the
            # series poster (never a random episode thumbnail).
            if series_cover:
                meta["series_cover"] = series_cover
            # Expected final episode (last tab's end) — lets the bot verify
            # the last episode made it into the DB after download.
            if isinstance(tab_texts, list) and tab_texts:
                _, last_end = _tab_range(str(tab_texts[-1]))
                if last_end:
                    meta["last_ep_num"] = last_end
            # The video currently open on the page — the bot's automatic
            # fill source when the final episode is missing.
            if last_page_video_id:
                meta["last_ep_url"] = f"https://www.tiktok.com/@{username}/video/{last_page_video_id}"
            episodes.insert(0, {"_meta": meta})
        return episodes


# ── Video URL extraction (Playwright fetch) ──────────────────────────────────

async def extract_video_url(
    url: str,
    *,
    headless: bool = True,
) -> dict | None:
    """Open a TikTok video page with Playwright and extract the direct MP4 URL.
    
    Returns a dict matching the shape of ``telegram_bot._fetch_video_*()`` output,
    or None on failure.  This is used as the final fallback downloader when
    all other methods (ssstik.io, TikWM, yt-dlp) fail.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser, context = await create_playwright_context(p, headless=headless)
        page = await context.new_page()

        video_data = {}
        video_bytes = None
        video_content_type = "video/mp4"
        video_url_found = None

        # Intercept video responses from TikTok CDN
        async def on_response(response):
            nonlocal video_bytes, video_content_type, video_url_found
            url = response.url
            if ('tiktokcdn.com' in url or 'tikcdn' in url) and ('video' in url or '.mp4' in url):
                if video_bytes is None:
                    ctype = response.headers.get("content-type", "")
                    if 'video' in ctype or 'octet' in ctype or 'mp4' in ctype:
                        try:
                            body = await response.body()
                            if len(body) > 10000:
                                video_bytes = body
                                video_content_type = ctype or "video/mp4"
                                video_url_found = url
                                log.info("Playwright direct: captured %d bytes from CDN %s", len(body), url[:80])
                        except Exception:
                            pass

        page.on('response', on_response)

        try:
            log.info("Playwright direct: navigating to %s", url)
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await _random_sleep(5.0, 10.0)

            # Extract metadata from rehydration data
            data = await page.evaluate("""() => {
                const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                if (!s) return null;
                try {
                    const d = JSON.parse(s.textContent);
                    const item = d.__DEFAULT_SCOPE__?.['webapp.video-detail']?.itemInfo?.itemStruct;
                    if (!item) return null;
                    let vu = '';
                    const pa = item.video?.playAddr;
                    if (typeof pa === 'string' && pa.startsWith('http')) {
                        vu = pa;
                    } else if (Array.isArray(pa) && pa.length > 0) {
                        vu = pa[pa.length - 1]?.src || pa[0]?.src || '';
                    }
                    if (!vu) {
                        const da = item.video?.downloadAddr;
                        if (typeof da === 'string' && da.startsWith('http')) vu = da;
                    }
                    const author = item.author?.uniqueId || item.author?.nickname || '';
                    return {
                        video_url: vu,
                        title: item.desc || '',
                        description: item.desc || '',
                        thumbnail: item.video?.cover || item.video?.originCover || item.author?.avatarLarger || '',
                        duration: item.video?.duration || 1,
                        username: author,
                    };
                } catch(e) { return null; }
            }""")

            if data:
                video_data = data
                if not video_url_found and data.get("video_url"):
                    video_url_found = data["video_url"]
            else:
                log.warning("Playwright direct: no rehydration data")
                return None

            # If we didn't capture bytes via response interception, try navigating to CDN URL
            if not video_bytes and video_url_found:
                log.info("Playwright direct: navigating directly to CDN URL to capture bytes")
                try:
                    resp = await page.goto(video_url_found, timeout=60000, wait_until="domcontentloaded")
                    if resp:
                        body = await resp.body()
                        ctype = resp.headers.get("content-type", "video/mp4")
                        if len(body) > 10000:
                            video_bytes = body
                            video_content_type = ctype
                            log.info("Playwright direct: got %d bytes from direct CDN navigation", len(body))
                except Exception as e:
                    log.warning("Playwright direct: CDN navigation failed: %s", e)

            video_data["_video_bytes"] = video_bytes
            video_data["_content_type"] = video_content_type if video_bytes else "video/mp4"
            video_data.setdefault("webpage_url", url)
            video_data.setdefault("title", "Untitled")
            video_data.setdefault("description", "")
            video_data.setdefault("thumbnail", "")
            video_data.setdefault("duration", 1)
            video_data.setdefault("username", "")

            log.info("Playwright direct: result for %s — URL: %s, bytes: %d",
                     url, (video_url_found or "NONE")[:80], len(video_bytes) if video_bytes else 0)
            return video_data

        except Exception as e:
            log.error("Playwright direct extraction failed for %s: %s", url, e)
            return None
        finally:
            await browser.close()


def extract_video_sync(url: str, **kw) -> dict | None:
    """Synchronous wrapper around ``extract_video_url()``."""
    return asyncio.run(extract_video_url(url, **kw))


# ── Synchronous wrapper for bot/webhook ──────────────────────────────────────

def _safe_stdout():
    """Make stdout UTF-8-safe so ``print()`` with non-ASCII text (e.g. the
    "Episode N/M found -> id" lines) never crashes on a legacy console
    codepage (cp1251) or a None stdout (pythonw)."""
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def extract_episodes_sync(url: str, **kw) -> list[dict]:
    """Synchronous wrapper around ``extract_episodes()``."""
    _safe_stdout()
    return asyncio.run(extract_episodes(url, **kw))


# ── Login helper ─────────────────────────────────────────────────────────────

async def _do_login():
    """Open a browser for the user to manually log in to TikTok.
    
    Save storage state when done so subsequent automated runs are authenticated.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser, context = await create_playwright_context(p, headless=False)
        page = await context.new_page()

        print("=" * 60)
        print("TikTok Login - Please log in to TikTok in the opened browser.")
        print("After logging in successfully, close the browser window.")
        print("=" * 60)

        await page.goto("https://www.tiktok.com/login", timeout=30000, wait_until="domcontentloaded")
        print("Browser opened. Complete login, then close the browser.")

        while True:
            try:
                if not page or page.is_closed():
                    break
                await asyncio.sleep(1)
                current = page.url
                if "login" not in current and "tiktok.com" in current:
                    await asyncio.sleep(3)
                    if "login" not in page.url:
                        print("Login detected! Saving storage state...")
                        break
            except Exception:
                break

        await save_storage(context)
        await browser.close()

    if _storage_exists():
        print(f"[OK] Storage state saved to {STORAGE_STATE}")
        print("  The Telegram bot will now use this authenticated session.")
    else:
        print("[FAIL] Storage state was NOT saved. Login may have failed.")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
    )
    _safe_stdout()

    if "--login" in sys.argv:
        asyncio.run(_do_login())
    elif "--url" in sys.argv:
        idx = sys.argv.index("--url")
        if idx + 1 < len(sys.argv):
            target_url = sys.argv[idx + 1]
            result = extract_episodes_sync(target_url, headless=('--visible' not in sys.argv))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("Usage: python scripts/tiktok_playwright.py --url <tiktok_url> [--debug] [--visible]")
            sys.exit(1)
    else:
        print("Usage:")
        print("  python scripts/tiktok_playwright.py --login               # one-time login")
        print("  python scripts/tiktok_playwright.py --url <URL>            # extract episodes")
        print("  python scripts/tiktok_playwright.py --url <URL> --debug    # + dump page structure + screenshot")
