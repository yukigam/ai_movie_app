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
    attempts = 2 if quick else 4
    for attempt in range(attempts):
        try:
            resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=timeout)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            log.warning("Page fetch failed for %s: %s", url, str(e)[:120])
            return None
        if len(html) >= 50000:
            break
        # Thin shell — retry after a short cooldown (TikTok alternates
        # full page / login-wall shell per request).  In quick mode keep
        # the wait tiny so the parallel walk stays fast.
        if attempt < attempts - 1:
            import time as _t
            _t.sleep(0.6 if quick else 2.0 + attempt * 1.5)
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


def _select_drama_episodes(entries, username, drama_id, expected, progress_cb=None):
    """Walk the account's video pages over ONE persistent HTTP session and
    keep ONLY the videos that belong to the requested drama (by ``dramaID``),
    ordered by their official episode number.

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
    pending = list(vids)
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
        return None

    # Entries come newest-first; episode order must be oldest-first.  When
    # timestamps are present they are authoritative (release order == watch
    # order for short dramas), otherwise mirror the page order on return.
    stamps = [e.get("timestamp") or 0 for e in entries]
    has_stamps = sum(1 for s in stamps if s) >= len(entries) // 2
    if has_stamps:
        entries.sort(key=lambda e: e.get("timestamp") or 0)
    else:
        entries.reverse()
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
    if "/video/" in url:
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
        prog(f"@{username} — ангиудыг цуглуулж байна…")
        entries = _profile_entries(username, meta.get("sec_uid"))
        if entries:
            drama_id = meta.get("drama_id")
            expected = meta.get("expected") or 0
            if drama_id and expected:
                # Official dramaID + official total known — ONLY aligned
                # videos are imported: walk the account pages and HARD-STOP
                # the moment the official total (e.g. 50) is collected.
                # The account may hold 200-300 unrelated videos, and
                # episodes numbered above the official total are rejected —
                # none of that junk ever reaches the import list.
                prog(f"Цувралын албан ёсны ангийн тоо: {expected} — "
                     f"илүүг татахгүй, шүүж эхэлж байна…")
                selected = _select_drama_episodes(
                    entries, username, drama_id, expected, progress_cb)
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