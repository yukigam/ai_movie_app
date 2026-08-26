#!/usr/bin/env python3
"""
Pure-HTTP TikTok series extraction — NO browser, NO Playwright, no Chromium.

Memory footprint ~15 MB: the whole flow is a couple of plain httpx GETs
(static page HTML → official ``dramaInfo`` metadata + author id) plus
yt-dlp's TikTok extractor walking the account's public video list via the
``/api/post/item_list`` endpoint (cursor pagination handled internally,
X-Bogus signing included — zero RAM, unlike a browser).

Pipeline
--------
1. ``_load_page(url)``          → video page HTML → rehydration JSON
2. ``_drama_meta(scope)``       → OFFICIAL series name, poster, episode count
                                 (``dramaInfo`` block on short-drama pages —
                                 NEVER the @account name)
3. ``_profile_entries(user)``   → yt-dlp ``TikTokUser`` → ALL video ids in
                                 release order (oldest first)
4. ``extract_series(url)``      → `[{"_meta": {...}}, {"episode", "url"}, …]`

The dramaInfo episode count (``numVideos``) doubles as the completeness
gate: when the account's video list length matches it, the import can be
reported complete; otherwise the bot's existing partial-warning logic kicks
in and the user just re-sends the link to top up.

Authentication is optional: any of the usual cookie files (Render secret
files, project root ``cookies.txt`` or ``$TIKTOK_COOKIES_FILE``) is used as
the ``cookiefile`` for yt-dlp and the ``Cookie`` header for httpx.  Without
cookies TikTok still serves the static HTML; only stricter rate limits may
apply, which the caller's retry logic absorbs.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

log = logging.getLogger("ttseries")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
REHYDRATION_RE = re.compile(
    r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.S,
)
_USERNAME_RE = re.compile(r"tiktok\.com/@([\w.-]+)")
# Short-drama deep link: /shortdrama/episode/{dramaID}/{episodeNum}
# (the webapp route found in TikTok's own JS bundles — the first number
# IS the dramaID, confirmed against /api/drama/detail/)
_SD_EPISODE_RE = re.compile(r"/shortdrama/episode/(\d{15,25})(?:/(\d+))?", re.I)
MAX_ACCOUNT_VIDEOS = 400  # hard cap — distributor accounts exceed 200


def _cookie_candidates() -> list[str]:
    """Priority order of Netscape cookie files to use for authenticated
    requests (Render secret mounts + local dev root)."""
    paths: list[str] = []
    env = os.getenv("TIKTOK_COOKIES_FILE")
    if env:
        paths.append(env)
    paths.append(os.path.join(PROJECT_ROOT, "cookies.txt"))
    paths.append(os.path.join(PROJECT_ROOT, "scripts", "cookies.txt"))
    paths.append("/app/cookies.txt")
    paths.append("/etc/secret_files/cookies.txt")
    paths.append("/etc/secrets/cookies.txt")
    return paths


def _cookie_file() -> str | None:
    for p in _cookie_candidates():
        try:
            if p and os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


def _cookies_header() -> str:
    """Build a ``Cookie`` header string from the best available cookie file."""
    cf = _cookie_file()
    if not cf:
        return ""
    pairs = []
    try:
        with open(cf, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
    except OSError:
        return ""
    return "; ".join(pairs)


def clean_url(url: str) -> str:
    """Strip tracking query params (they force TikTok to serve CAPTCHAs)."""
    return url.split("?")[0].rstrip("/")


def _rotate_host(url: str) -> str:
    """Swap www.tiktok.com <-> m.tiktok.com — automatic mirror fallback
    for DNS / [Errno -2] / getaddrinfo failures on one host."""
    m = re.search(r"(https?://)([\w.-]*tiktok\.com)", url)
    if not m:
        return url
    host = m.group(2)
    swap = "m.tiktok.com" if host.startswith("www") else "www.tiktok.com"
    return url[:m.start(2)] + swap + url[m.end(2):]


def _load_page(url: str, timeout: float = 25.0, quick: bool = False) -> dict | None:
    """Fetch a TikTok page over plain HTTP and return the rehydration scope
    dict, or None when the page carries no JSON (thin shell page, network
    error, captcha wall).  TikTok alternates between a ~43 KB shell and the
    full ~430 KB page per request, so thin shells are retried with a small
    backoff before giving up (unless *quick* — used by the bulk per-video
    walk, where a miss is cheap and just skipped).  Never raises."""
    import httpx

    url = clean_url(url)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    cookie = _cookies_header()
    if cookie:
        headers["Cookie"] = cookie
    attempts = 2 if quick else 6
    mirrored = False
    for attempt in range(attempts):
        try:
            resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=timeout)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            msg = str(e)[:120].lower()
            # DNS/[Errno -2]/getaddrinfo — rotate to the mirror host once
            # instead of failing the whole page fetch.
            if (not mirrored and ("name or service not known" in msg
                                  or "getaddrinfo" in msg
                                  or "connection" in msg
                                  or isinstance(e, (ConnectionError, TimeoutError)))):
                url = _rotate_host(url)
                mirrored = True
                log.warning("Page fetch DNS/conn error — rotating to mirror %s: %s",
                            url, str(e)[:100])
                continue
            log.warning("Page fetch failed for %s: %s", url, str(e)[:120])
            return None
        if len(html) >= 50000:
            break
        # Thin shell — retry after a cooldown (TikTok alternates full page /
        # login-wall shell per request; some IPs get long streaks of shells,
        # so escalate patiently up to ~30s total instead of giving up fast).
        if attempt < attempts - 1:
            import time as _t
            _t.sleep(0.6 if quick else min(3.0 + attempt * 3.0, 12.0))
    else:
        log.info("Page %s stays a thin shell after retries — no rehydration data", url)
        return None
    m = REHYDRATION_RE.search(html)
    if not m:
        log.info("No rehydration script on %s", url)
        return None
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        log.warning("Rehydration JSON parse failed on %s: %s", url, str(e)[:80])
        return None
    return (data or {}).get("__DEFAULT_SCOPE__") or {}


def _drama_meta(scope: dict) -> dict:
    """Extract the OFFICIAL short-drama metadata from the page's
    ``itemStruct.dramaInfo`` block: real series name, poster, episode count.

    Also returns the author's unique id — the key for the full episode list.
    """
    meta: dict = {}
    vd = scope.get("webapp.video-detail") or {}
    item = (vd.get("itemInfo") or {}).get("itemStruct") or {}
    if not isinstance(item, dict):
        return meta
    author = item.get("author") or {}
    if isinstance(author, dict):
        if author.get("uniqueId"):
            meta["username"] = str(author["uniqueId"])
        if author.get("secUid"):
            meta["sec_uid"] = str(author["secUid"])
    if item.get("id"):
        meta["current_id"] = str(item["id"])
    # Fallback: the single video's own fields (before dramaInfo wins)
    title = str(item.get("title") or "").strip()
    if not title:
        title = str(item.get("desc") or "").split("\n")[0].strip()
    cover = ""
    vid_blob = item.get("video")
    if isinstance(vid_blob, dict):
        cv = vid_blob.get("cover")
        if isinstance(cv, dict):
            clist = cv.get("urlList") or []
            if clist:
                cover = str(clist[0])
    di = item.get("dramaInfo") or {}
    if isinstance(di, dict):
        dname = str(di.get("dramaName") or "").strip()
        if dname:
            title = dname
        if di.get("dramaID"):
            meta["drama_id"] = str(di["dramaID"])
        try:
            expected = int(di.get("numVideos") or 0)
        except (TypeError, ValueError):
            expected = 0
        if expected:
            meta["expected"] = expected
        dlist = (di.get("cover") or {}).get("UrlList") or []
        if dlist:
            cover = str(dlist[0])
    if title:
        meta["series_title"] = title
    if cover:
        meta["series_cover"] = cover
    return meta


def _drama_matches(scope: dict, drama_id: str | None) -> dict | None:
    """Return the video's short-drama block only when it belongs to the
    target drama (``drama_id`` match), else None."""
    vd = scope.get("webapp.video-detail") or {}
    item = (vd.get("itemInfo") or {}).get("itemStruct") or {}
    if not isinstance(item, dict):
        return None
    di = item.get("dramaInfo")
    if not isinstance(di, dict):
        return None
    if drama_id and str(di.get("dramaID") or "") != str(drama_id):
        return None
    return di


_ITEM_LIST_API = "https://www.tiktok.com/api/creator/item_list/"
_DRAMA_EPISODES_API = "https://www.tiktok.com/api/drama/episode/item_list/"
_DRAMA_DETAIL_API = "https://www.tiktok.com/api/drama/detail/"


def _api_drama_detail(drama_id: str) -> dict:
    """Official drama metadata from ``/api/drama/detail/`` (no signature).

    Returns the same shape ``_drama_meta`` produces — series_title,
    series_cover, drama_id, expected (numVideos), username, sec_uid —
    so a /shortdrama/episode/ deep link resolves WITHOUT loading any
    video page.  {} when TikTok refuses.
    """
    import random as _rand
    import string as _string

    query = {
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "device_platform": "web_pc",
        "language": "en",
        "os": "windows",
        "priority_region": "",
        "region": "US",
        "tz_name": "UTC",
        "webcast_language": "en",
        "device_id": str(_rand.randint(7250000000000000000, 7325099899999994577)),
        "verifyFp": "verify_" + "".join(_rand.choices(_string.hexdigits, k=7)),
        "dramaID": str(drama_id),
    }
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.tiktok.com/",
    }
    cookie = _cookies_header()
    if cookie:
        headers["Cookie"] = cookie
    try:
        import httpx
        resp = httpx.get(_DRAMA_DETAIL_API, params=query, headers=headers,
                         follow_redirects=True, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        code = int(data.get("statusCode") or data.get("status_code") or 0)
        if code != 0:
            log.warning("Drama detail API statusCode=%s for %s", code, drama_id)
            return {}
        di = data.get("dramaInfo") or {}
        if not isinstance(di, dict) or not di.get("dramaName"):
            return {}
        meta: dict = {
            "series_title": str(di["dramaName"]).strip(),
            "drama_id": str(di.get("dramaID") or drama_id),
        }
        try:
            expected = int(di.get("numVideos") or 0)
        except (TypeError, ValueError):
            expected = 0
        if expected:
            meta["expected"] = expected
        clist = ((di.get("cover") or {}).get("urlList")) or []
        if clist:
            meta["series_cover"] = str(clist[0])
        user = ((di.get("author") or {}).get("user")) or {}
        if user.get("uniqueId"):
            meta["username"] = str(user["uniqueId"])
        if user.get("secUid"):
            meta["sec_uid"] = str(user["secUid"])
        desc = str(di.get("description") or "").strip()
        if desc:
            meta["description"] = desc
        log.info("Drama detail API: '%s' (%s episodes) by @%s",
                 meta["series_title"], expected, meta.get("username"))
        return meta
    except Exception as e:
        log.warning("Drama detail API failed for %s: %s", drama_id, str(e)[:120])
        return {}


def _api_drama_episodes(drama_id: str, expected: int) -> list[dict]:
    """Official short-drama episode list straight from TikTok's own API.

    ``GET /api/drama/episode/item_list/?dramaID=…&cursor=0&count=N`` is
    the exact endpoint the web app's Episodes sidebar calls (found in the
    2026 webapp JS bundles).  ONE request returns every episode with its
    OFFICIAL number (``dramaInfo.DramaVideoData.EpisodeNumber``) — even
    though drama-episode videos are hidden from ``creator/item_list``.
    No signature (X-Bogus) required.

    Returns ``[{episode, id, create_time}, …]`` ordered by episode number,
    clamped to *expected*; [] when TikTok refuses (param drift/rate limit).
    """
    import random as _rand
    import string as _string

    query = {
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "video",
        "language": "en",
        "os": "windows",
        "priority_region": "",
        "region": "US",
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "UTC",
        "webcast_language": "en",
        "device_id": str(_rand.randint(7250000000000000000, 7325099899999994577)),
        "verifyFp": "verify_" + "".join(_rand.choices(_string.hexdigits, k=7)),
        "dramaID": str(drama_id),
        "cursor": "0",
        "count": str(max(int(expected or 0), 20)),
    }
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    cookie = _cookies_header()
    if cookie:
        headers["Cookie"] = cookie
    try:
        import httpx
    except ImportError:
        return []

    out: list[dict] = []
    cursor = 0
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=20) as cli:
            for _page in range(10):
                q = dict(query)
                q["cursor"] = str(cursor)
                resp = cli.get(_DRAMA_EPISODES_API, params=q)
                resp.raise_for_status()
                data = resp.json()
                try:
                    code = int(data.get("statusCode") or data.get("status_code") or 0)
                except (TypeError, ValueError):
                    code = 0
                if code != 0:
                    log.warning("Drama episode API statusCode=%s (%s)",
                                code, str(data.get("status_msg"))[:60])
                    break
                batch = data.get("itemList") or []
                for it in batch:
                    if not isinstance(it, dict):
                        continue
                    vid = str(it.get("id") or "")
                    if not vid.isdigit():
                        continue
                    di = it.get("dramaInfo") or {}
                    dvd = di.get("DramaVideoData") or {}
                    try:
                        ep = int(dvd.get("EpisodeNumber"))
                    except (TypeError, ValueError):
                        ep = len(out) + 1  # sequence position fallback
                    out.append({
                        "episode": ep,
                        "id": vid,
                        "create_time": it.get("createTime"),
                    })
                if not data.get("hasMore") or not batch:
                    break
                cursor += len(batch)
                if expected and len(out) >= expected:
                    break
    except Exception as e:
        log.warning("Drama episode API failed: %s", str(e)[:120])
        return out

    # De-duplicate per episode number (first wins), clamp to expected.
    by_ep: dict[int, dict] = {}
    for e in out:
        by_ep.setdefault(e["episode"], e)
    out = [by_ep[n] for n in sorted(by_ep)]
    if expected:
        out = [e for e in out if 1 <= e["episode"] <= expected]
    log.info("Drama episode API: %d episodes for dramaID=%s", len(out), drama_id)
    return out


def _drama_episodes_via_desc(items: list[dict], title: str, num_videos: int,
                            username: str) -> list[dict]:
    """Identify a drama's episodes by CAPTION — no SSR page walk needed.

    Short-drama accounts post every episode with the drama's title leading
    the caption ('Hired by My Billionaire Baby Daddy#film#drama…',
    '《A Vow of Two Lifetimes》#drama…').  Verified pattern: all episodes of
    one drama share the title phrase, and release order (createTime) ==
    episode order.  When the caption itself carries an episode number
    ('EP.12', 'Episode 4', 'Lesson5') it is preferred.

    Returns ``[{episode, url}, …]`` (1..n, clamped to *num_videos*) or []
    when nothing matches.
    """
    import re as _re

    norm = _re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    words = [w for w in norm.split() if len(w) >= 3]
    if not words:
        return []

    cands: list[dict] = []
    for it in items:
        desc = str(it.get("desc") or "")
        dnorm = _re.sub(r"[^a-z0-9]+", " ", desc.lower())
        if not dnorm:
            continue
        hit = sum(1 for w in words if w in dnorm)
        if hit >= max(2, int(len(words) * 0.8)):
            cands.append(it)
    if not cands:
        return []

    by_num: dict[int, str] = {}
    by_pos: list[dict] = []
    seen: set[str] = set()
    cands.sort(key=lambda it: int(it.get("createTime") or 0))
    for it in cands:
        vid = str(it.get("id") or "")
        mm = _re.search(r"(\d{15,25})", vid)
        vid = mm.group(1) if mm else vid
        if not vid or vid in seen:
            continue
        seen.add(vid)
        m = _re.search(r"(?:EP\.?|Episode|Ep|Part|Lesson)\s*[:\-]?\s*(\d+)",
                       str(it.get("desc") or ""), _re.I)
        if m:
            by_num.setdefault(int(m.group(1)), vid)
        else:
            by_pos.append({"id": vid, "ts": int(it.get("createTime") or 0)})

    if len(by_num) >= num_videos:
        found = [{
            "episode": n,
            "url": "https://www.tiktok.com/@{0}/video/{1}".format(username, v),
        } for n, v in sorted(by_num.items())]
    else:
        # Positional ordering within the drama's post block.
        merged: dict[int, str] = dict(by_num)
        for pos in by_pos:
            next_n = (max(merged) + 1) if merged else 1
            while next_n in merged:
                next_n += 1
            merged[next_n] = pos["id"]
        found = [{
            "episode": n,
            "url": "https://www.tiktok.com/@{0}/video/{1}".format(username, v),
        } for n, v in sorted(merged.items())]

    found = found[:num_videos]
    if found:
        log.info("Caption-based drama grouping: %d/%d episodes matched '%s'",
                 len(found), num_videos, title[:40])
    return found


def _api_item_list(username: str, sec_uid: str,
                   max_videos: int = MAX_ACCOUNT_VIDEOS) -> list[dict]:
    """Bulk listing of an account's videos straight from TikTok's
    ``creator/item_list`` API — ONE request per ~15 videos, the same
    endpoint yt-dlp uses internally (verifyFp/device_id params; NO
    X-Bogus signature needed).

    Returns the raw itemStruct dicts: each carries ``id``, ``createTime``
    and — for short dramas — the SAME ``dramaInfo`` block the per-video
    pages embed, including the official episode number.  So all ~N episode
    IDs of a drama arrive in a handful of API calls instead of a 1-by-1
    page walk that TikTok rate-limits into a crawl (the "only 1/50"
    failure).  Returns [] when TikTok refuses the raw calls.
    """
    import random as _rand
    import string as _string
    import time as _time

    query = {
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "count": "15",
        "cursor": "0",
        "device_id": str(_rand.randint(7250000000000000000, 7325099899999994577)),
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "user",
        "history_len": "2",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": "en",
        "os": "windows",
        "priority_region": "",
        "referer": "",
        "region": "US",
        "screen_height": "1080",
        "screen_width": "1920",
        "secUid": sec_uid or "",
        "type": "1",
        "tz_name": "UTC",
        "verifyFp": "verify_" + "".join(_rand.choices(_string.hexdigits, k=7)),
        "webcast_language": "en",
    }
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/@{0}".format(username),
    }
    cookie = _cookies_header()
    if cookie:
        headers["Cookie"] = cookie
    try:
        import httpx
    except ImportError:
        return []

    items: list[dict] = []
    cursor = int(_time.time() * 1e3)
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=20) as cli:
            for _page in range(20):  # 15/page → up to 300 videos
                q = dict(query)
                q["cursor"] = str(cursor)
                api_url = _ITEM_LIST_API
                mirrored_api = False
                data = None
                for _try in range(3):  # DNS/connection drift -> retry in place
                    try:
                        resp = cli.get(api_url, params=q)
                        resp.raise_for_status()
                        data = resp.json()
                        break
                    except Exception as e:
                        msg = str(e)[:100]
                        if _try < 2 and (
                            "name or service not known" in msg.lower()
                            or "getaddrinfo" in msg.lower()
                            or isinstance(e, (ConnectionError, TimeoutError))
                        ):
                            if not mirrored_api:
                                api_url = _rotate_host(api_url)
                                mirrored_api = True
                            log.warning("item_list API page %d transient error (try %d/3): %s — retrying",
                                        _page + 1, _try + 2, msg)
                            _time.sleep(2.0 * (_try + 1))
                            continue
                        log.warning("item_list API page %d failed: %s",
                                    _page + 1, msg)
                        data = None
                        break
                if data is None:
                    break
                batch = data.get("itemList") or []
                items.extend(b for b in batch
                             if isinstance(b, dict) and b.get("id"))
                more = bool(data.get("hasMorePrevious"))
                if not more or len(items) >= max_videos:
                    break
                last = batch[-1] if batch else None
                new_cursor = 0
                if isinstance(last, dict):
                    try:
                        new_cursor = int(float(last.get("createTime") or 0) * 1e3)
                    except (TypeError, ValueError):
                        new_cursor = 0
                if not batch:
                    # Rate-limit window: TikTok occasionally serves an empty
                    # page while hasMorePrevious stays true.  Never break on
                    # that (it silently drops the NEWEST videos — the very
                    # episodes an ongoing drama imports need); step back a
                    # week and keep going.
                    log.warning("item_list API page %d empty (hasMorePrevious=%s) — "
                                "stepping cursor back a week", _page + 1, more)
                    new_cursor = int(float(cursor) - 7 * 86400 * 1e3)
                if not new_cursor or new_cursor == cursor or new_cursor <= 0:
                    break
                cursor = new_cursor
                _time.sleep(_rand.uniform(0.4, 0.9))
    except Exception as e:
        log.warning("item_list API failed entirely: %s", str(e)[:100])
        return []
    if items:
        log.info("Bulk item_list API: %d items from @%s", len(items), username)
    return items


def _select_drama_episodes(entries, username, drama_id, expected, progress_cb=None,
                           sec_uid: str | None = None):
    """Walk the account's video pages over ONE persistent HTTP session and
    keep ONLY the videos that belong to the requested drama (by ``dramaID``),
    ordered by their official episode number.

    FAST PATH FIRST: when the account's secUid is known, the full item
    list is pulled from the bulk ``creator/item_list`` API (a handful of
    requests) so the account is fully enumerated even when yt-dlp's
    profile extractor is blocked.  NOTE: the bulk payload carries NO
    ``dramaInfo`` (2026 API) — the per-video page walk is therefore the
    real episode-number source and runs over every video unless the bulk
    API managed to number episodes directly.

    Covers multi-PAGE (tabbed) dramas — TikTok splits long series into
    episode groups ("1-24", "25-48", ...) in the sidebar — because the
    per-video ``dramaInfo`` block carries the dramaID and official
    EpisodeNumber regardless of which tab a video belongs to.  The walk is
    sequential (a hard parallel burst makes TikTok flip every page to the
    thin login shell) with two rounds: a first pass at modest pace, then a
    quieter retry pass over only the pages that came back thin/errored.
    Stops as soon as *expected* episodes are collected."""
    import time as _time
    import random as _rand

    def vid_of(e):
        vid = str(e.get("id") or "")
        mm = re.search(r"(\d{15,25})", vid)
        return mm.group(1) if mm else None

    vids = [v for v in (vid_of(e) for e in entries) if v]

    try:
        import httpx
    except ImportError:
        log.warning("httpx unavailable — cannot walk pages for drama selection")
        return []

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    cookie = _cookies_header()
    if cookie:
        headers["Cookie"] = cookie

    def probe(cli, vid) -> dict | None:
        """Fetch one video page: returns the scope dict or None."""
        url = "https://www.tiktok.com/@{0}/video/{1}".format(username, vid)
        for _attempt in range(3):
            try:
                resp = cli.get(url)
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                log.warning("Selection fetch failed for %s: %s", url, str(e)[:120])
                _time.sleep(2.0)
                continue
            if len(html) >= 50000:
                mm = REHYDRATION_RE.search(html)
                if mm:
                    try:
                        data = json.loads(mm.group(1))
                        return (data or {}).get("__DEFAULT_SCOPE__") or {}
                    except Exception:
                        return None
                return None
            _time.sleep(2.0)  # thin shell — quiet retry
        return None

    def match_ep(scope) -> int | None:
        di = _drama_matches(scope, drama_id)
        if not di:
            return None
        try:
            ep = int(((di.get("DramaVideoData") or {}).get("EpisodeNumber")) or 0)
        except (TypeError, ValueError):
            ep = 0
        if ep < 1 or ep > expected:
            return None  # official numbers are 1..N — never import beyond the drama total
        return ep

    by_num: dict[int, str] = {}
    api_fetched: set[str] = set()
    if sec_uid:
        for item in _api_item_list(username, sec_uid):
            vid = str(item.get("id") or "")
            mm = re.search(r"(\d{15,25})", vid)
            if not mm:
                continue
            vid = mm.group(1)
            api_fetched.add(vid)
            if len(by_num) >= expected:
                continue
            di = item.get("dramaInfo")
            if not isinstance(di, dict):
                continue
            if str(di.get("dramaID") or "") != str(drama_id):
                continue
            try:
                ep = int(((di.get("DramaVideoData") or {}).get("EpisodeNumber")) or 0)
            except (TypeError, ValueError):
                ep = 0
            if 1 <= ep <= expected and ep not in by_num:
                by_num[ep] = vid
                if progress_cb:
                    try:
                        progress_cb("Энэ цувралын ангиудыг цуглуулж байна: "
                                    "{0}/{1}...".format(len(by_num), expected))
                    except Exception:
                        pass
        if by_num:
            log.info("Bulk API resolved %d/%d episodes directly",
                     len(by_num), expected)
        else:
            # Issue in the wild: TikTok's item_list payload does NOT carry
            # a dramaInfo block (2026: itemStruct has no dramaInfo key) —
            # only the per-video page rehydration has the episode number.
            # In that case the walk MUST cover every video; otherwise the
            # api_fetched set would skip the walk and return 0 episodes.
            if api_fetched:
                log.info("Bulk API carried no dramaInfo for this drama — "
                         "falling back to the full per-video page walk (%d videos)",
                         len(api_fetched))
                api_fetched = set()

    pending = [v for v in vids if v not in api_fetched]
    if pending:
        log.info("Drama walk continues over %d videos not covered by the bulk API",
                 len(pending))
    round_no = 0
    wall_start = _time.time()
    with httpx.Client(headers=headers, follow_redirects=True, timeout=20) as cli:
        while pending and len(by_num) < expected and _time.time() - wall_start < 420:
            round_no += 1
            scale = 1.0 if round_no == 1 else 2.5  # quieter second round
            missed: list[str] = []
            for vid in pending:
                if len(by_num) >= expected or _time.time() - wall_start >= 420:
                    break
                _time.sleep(_rand.uniform(0.8, 1.4) * scale)
                scope = probe(cli, vid)
                if not scope:
                    missed.append(vid)
                    continue
                ep = match_ep(scope)
                if ep is None or ep in by_num:
                    continue
                by_num[ep] = vid
                if progress_cb:
                    try:
                        progress_cb("Энэ цувралын ангиудыг цуглуулж байна: "
                                    "{0}/{1}...".format(len(by_num), expected))
                    except Exception:
                        pass
            if round_no == 1:
                pending = missed[:120]  # second round capped
            else:
                pending = []

    found = [{
        "episode": num,
        "url": "https://www.tiktok.com/@{0}/video/{1}".format(username, vid),
    } for num, vid in sorted(by_num.items())]
    return found


def _profile_entries(username: str, sec_uid: str | None = None) -> list[dict] | None:
    """List ALL public videos of an account via yt-dlp's TikTok user
    extractor (pure Python, paginated ``item_list`` API) - returns entries
    in RELEASE order (oldest first), each with at least ``id``/``url``.
    Returns None when the account cannot be read.
    """
    try:
        import yt_dlp
    except ImportError:
        log.warning("yt-dlp not installed — cannot read profiles")
        return None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": MAX_ACCOUNT_VIDEOS,
    }
    cf = _cookie_file()
    if cf:
        opts["cookiefile"] = cf
        log.info("yt-dlp using cookies from %s", cf)

    targets = [f"https://www.tiktok.com/@{username}"]
    if sec_uid:
        targets.append(f"tiktokuser:{sec_uid}")

    entries: list[dict] = []
    for target in targets:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
            entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
            if entries:
                break
        except Exception as e:
            log.warning("yt-dlp profile fetch failed for %s: %s", target, str(e)[:200])
    if not entries:
        # yt-dlp gave nothing (rate-limited to a thin shell) — never give
        # up on the account without trying the bulk API once.
        return _profile_entries_api_only(username, sec_uid)

    # Entries come newest-first; episode order must be oldest-first.  When
    # timestamps are present they are authoritative (release order == watch
    # order for short dramas), otherwise mirror the page order on return.
    stamps = [e.get("timestamp") or 0 for e in entries]
    has_stamps = sum(1 for s in stamps if s) >= len(entries) // 2
    if has_stamps:
        entries.sort(key=lambda e: e.get("timestamp") or 0)
    else:
        entries.reverse()

    # Under-delivery fallback: when yt-dlp only scratched the surface
    # (e.g. 1 of 50 videos — the "only 1/50 episodes" symptom), merge the
    # bulk API's full item list in so the walk/select sees every ID.
    api_items = _api_item_list(username, sec_uid)
    if len(api_items) > len(entries):
        by_id: dict[str, dict] = {}
        for e in entries:
            vid = str(e.get("id") or e.get("url") or "")
            bw = re.search(r"(\d{15,25})", vid)
            key = bw.group(1) if bw else vid
            if key:
                by_id[key] = e
        added = 0
        for it in api_items:
            vid = str(it.get("id") or "")
            bw = re.search(r"(\d{15,25})", vid)
            key = bw.group(1) if bw else vid
            if not key or key in by_id:
                continue
            stamp = it.get("createTime") or 0
            by_id[key] = {
                "id": key,
                "url": "https://www.tiktok.com/@{0}/video/{1}".format(username, key),
                "timestamp": int(float(stamp)) if stamp else 0,
            }
            added += 1
        if added:
            entries = [by_id[k] for k in by_id]
            entries.sort(key=lambda e: e.get("timestamp") or 0)
            log.info("Profile list extended by bulk API: %d extra videos",
                     added)
    return entries


def _profile_entries_api_only(username: str,
                              sec_uid: str | None = None) -> list[dict] | None:
    """yt-dlp-less profile listing (bulk API only) — a last-resort path for
    when the yt-dlp extractor is blocked entirely."""
    items = _api_item_list(username, sec_uid or "")
    if not items:
        return None
    entries = []
    for it in items:
        vid = str(it.get("id") or "")
        bw = re.search(r"(\d{15,25})", vid)
        key = bw.group(1) if bw else vid
        if not key:
            continue
        stamp = it.get("createTime") or 0
        entries.append({
            "id": key,
            "url": "https://www.tiktok.com/@{0}/video/{1}".format(username, key),
            "timestamp": int(float(stamp)) if stamp else 0,
        })
    entries.sort(key=lambda e: e.get("timestamp") or 0)
    return entries


def extract_series(url: str, progress_cb=None) -> list[dict]:
    """Extract a full episode list + official metadata over pure HTTP.

    Returns ``[{"_meta": {...}}, {"episode": 1, "url": ...}, ...]`` (the
    ``_meta`` header matches the format the old Playwright path produced,
    so the bot's import pipeline is untouched).  Never raises — an
    unresolvable series degrades to the single currently-open video.

    ``progress_cb(str)`` (optional) receives human-readable progress text.
    """
    def prog(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    url = clean_url(url)
    prog("🔍 Татаж авч байна: хуудасны албан metas…")

    scope = None
    meta: dict = {}
    sd = _SD_EPISODE_RE.search(url)
    if sd:
        # Short-drama deep link (/shortdrama/episode/{dramaID}/{num}) —
        # the dramaID is IN the URL; two API calls resolve everything
        # (detail → title/cover/total/username, episodes → all IDs).
        prog("Short-drama холбоос илэрлээ…")
        try:
            sd_meta = _api_drama_detail(sd.group(1))
        except Exception as e:
            log.warning("Short-drama detail crashed: %s", str(e)[:100])
            sd_meta = {}
        if sd_meta:
            meta.update(sd_meta)
            if meta.get("series_title"):
                prog(f"Кино: {meta['series_title']}")
    elif "/video/" in url:
        scope = _load_page(url)
        if scope is not None:
            meta = _drama_meta(scope)
            if meta.get("series_title"):
                prog(f"Кино: {meta['series_title']}")

    # Account name falls back to the URL itself when the page was a thin
    # shell (no rehydration data served).
    username = meta.get("username") or ""
    m = _USERNAME_RE.search(url)
    if not username and m:
        username = m.group(1)

    episodes: list[dict] = []
    if username:
        drama_id = meta.get("drama_id")
        expected = meta.get("expected") or 0
        selected: list[dict] = []

        if drama_id and expected:
            # Official dramaID + official total known — ONLY aligned
            # videos are imported; HARD-STOP at the official total.
            prog(f"Цувралын албан ёсны ангийн тоо: {expected} — "
                 f"албан ёсны жагсаалтыг татаж байна…")
            # PRIMARY fast path: TikTok's own episode-list API — ONE
            # request returns ALL episodes with official numbering,
            # even though those videos are hidden from item_list.
            try:
                api_eps = _api_drama_episodes(drama_id, expected)
            except Exception as e:
                log.warning("Drama episode API crashed: %s", str(e)[:100])
                api_eps = []
            if api_eps:
                by_ep: dict[int, dict] = {}
                for e in api_eps:
                    by_ep.setdefault(e["episode"], e)
                selected = [
                    {"episode": n,
                     "url": "https://www.tiktok.com/@{0}/video/{1}".format(
                         username, by_ep[n]["id"])}
                    for n in sorted(by_ep)
                ]
                prog(f"Албан ёсны API-аас {len(selected)}/{expected} анги шууд ирлээ")

        if drama_id and expected and len(selected) >= expected:
            episodes = selected[:expected]  # hard stop at the official total
            prog(f"Энэ цувралтай {len(episodes)}/{expected} анги таарлаа")
        else:
            # Slow fallbacks — they need the account's video list first.
            prog(f"@{username} — ангиудыг цуглуулж байна…")
            entries = _profile_entries(username, meta.get("sec_uid"))
            if entries and drama_id and expected:
                # Secondary: caption-based grouping over the bulk item list
                # (some dramas carry the title in every caption; release
                # order == episode order).  No SSR walk.
                if len(selected) < expected and meta.get("sec_uid"):
                    try:
                        raw_items = _api_item_list(username, meta["sec_uid"])
                        if raw_items:
                            desc_sel = _drama_episodes_via_desc(
                                raw_items, meta.get("series_title") or "",
                                expected, username)
                            seen_ids = {e["url"].rsplit("/", 1)[-1]
                                        for e in selected}
                            for e in desc_sel:
                                vid = e["url"].rsplit("/", 1)[-1]
                                if vid not in seen_ids:
                                    selected.append(e)
                                    seen_ids.add(vid)
                    except Exception as e:
                        log.warning("Caption-based drama grouping failed: %s",
                                    str(e)[:100])
                if len(selected) < expected:
                    # Fill any gaps with the per-video page walk (SSR
                    # dramaInfo only exists on the page you open — 2026
                    # TikTok's SSR — so this covers captions it missed).
                    walked = _select_drama_episodes(
                        entries, username, drama_id, expected, progress_cb,
                        sec_uid=meta.get("sec_uid"))
                    if walked or not selected:
                        by_ep: dict[int, dict] = {
                            e["episode"]: e for e in walked
                        }
                        for e in selected:
                            by_ep.setdefault(e["episode"], e)
                        selected = [by_ep[n] for n in sorted(by_ep)][:expected]
                if selected:
                    episodes = selected[:expected]  # hard stop at the official total
                    prog(f"Энэ цувралтай {len(episodes)}/{expected} анги таарлаа")
                else:
                    # No video of this drama lives on the account — do NOT
                    # import the unrelated account list.
                    prog("⚠️ Энэ киноны ангиуд энэ хэрэглэгчээс олдсонгүй "
                         "(хамааралгүй видео импортлогдохгүй).")
            elif entries:
                # No drama signal at all (regular playlist/account) — the
                # whole account list in release order is the series.
                for i, e in enumerate(entries, 1):
                    vid = str(e.get("id") or e.get("url") or "")
                    if not vid:
                        continue
                    mm = re.search(r"/video/(\d+)", vid)
                    if mm:
                        vid = mm.group(1)
                    if not vid.isdigit():
                        continue
                    episodes.append({
                        "episode": i,
                        "url": f"https://www.tiktok.com/@{username}/video/{vid}",
                    })
                prog(f"Хэрэглэгчээс {len(episodes)} анги олдлоо")

    # Single-video fallback: the referenced video itself is EP 1.
    if not episodes and meta.get("current_id"):
        episodes.append({
            "episode": 1,
            "url": f"https://www.tiktok.com/@{username}/video/{meta['current_id']}"
                   if username else f"https://www.tiktok.com/video/{meta['current_id']}",
        })

    # Metadata header consumed by the bot's import pipeline.
    meta_out: dict = {}
    if meta.get("series_title"):
        meta_out["series_title"] = meta["series_title"]
    if meta.get("series_cover"):
        meta_out["series_cover"] = meta["series_cover"]
    # Account identity is carried too — the bot's top-up loop can then pull
    # ALL episode IDs from the bulk item_list API WITHOUT re-extracting.
    if meta.get("username"):
        meta_out["username"] = meta["username"]
    if meta.get("sec_uid"):
        meta_out["sec_uid"] = meta["sec_uid"]
    if meta.get("drama_id"):
        meta_out["drama_id"] = meta["drama_id"]
    expected = meta.get("expected") or 0
    if expected:
        meta_out["last_ep_num"] = expected
    elif episodes:
        meta_out["last_ep_num"] = len(episodes)
    if episodes:
        meta_out["last_ep_url"] = episodes[-1]["url"]
    result: list[dict] = []
    if meta_out:
        result.append({"_meta": meta_out})
    result.extend(episodes)
    log.info("extract_series(%s): %d episodes%s", url, len(episodes),
             f" (expected {expected})" if expected else "")
    return result


def extract_drama_meta(url: str) -> dict:
    """HTTP-only metadata helper: official title/poster/expected count for a
    TikTok video URL.  Returns {} when the page is unavailable."""
    scope = _load_page(url)
    if scope is None:
        return {}
    meta = _drama_meta(scope)
    if not meta:
        m = _USERNAME_RE.search(clean_url(url))
        if m:
            meta["username"] = m.group(1)
    if meta:
        log.info("drama meta for %s: '%s' (expected %s)",
                 clean_url(url), meta.get("series_title", ""),
                 meta.get("expected", "?"))
    return meta


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s",
                        level=logging.INFO)
    if "--url" in sys.argv:
        idx = sys.argv.index("--url")
        if idx + 1 < len(sys.argv):
            result = extract_series(sys.argv[idx + 1])
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("Usage: python scripts/tiktok_series.py --url <tiktok_url>")
            sys.exit(1)
    else:
        print("Usage:")
        print("  python scripts/tiktok_series.py --url <URL>   # extract series (HTTP only)")