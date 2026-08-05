"""Find 0-byte video files in Supabase Storage, delete them (and their DB
episode rows), then re-download & re-import them from TikTok.

Usage:
    python scripts/clean_and_redownload.py                    # cleanup only
    python scripts/clean_and_redownload.py <tiktok_url>       # cleanup + full series re-import
    python scripts/clean_and_redownload.py <url> --dry-run    # report only, delete nothing
"""

import argparse
import asyncio
import os
import re
import sys
import urllib.parse

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telegram_bot as bot  # noqa: E402  (loads .env, defines Store/_playlist)

_VIDEO_NAME_RE = re.compile(r"^(?P<folder>[^/]+)/video_(?P<num>\d+)\.mp4$")


def list_storage_objects(bucket: str) -> list[dict]:
    """Recursively page through every file in *bucket* (descends into folders).

    Folders are listed by Supabase without a ``metadata.size``; files always
    carry one (possibly 0).
    """
    url = f"{bot.SUPABASE_URL}/storage/v1/object/list/{bucket}"
    headers = {
        "apikey": bot.SUPABASE_KEY,
        "Authorization": f"Bearer {bot.SUPABASE_KEY}",
    }
    files: list[dict] = []
    seen: set[str] = set()

    def walk(prefix: str) -> None:
        offset = 0
        limit = 200
        while True:
            r = httpx.post(
                url,
                json={"prefix": prefix, "limit": limit, "offset": offset},
                headers=headers,
                timeout=60.0,
            )
            r.raise_for_status()
            page = r.json()
            for o in page:
                name = o.get("name", "")
                abs_name = prefix + name
                if (o.get("metadata") or {}).get("size") is None:
                    # Folder entry → descend into it
                    child = abs_name if abs_name.endswith("/") else abs_name + "/"
                    if child not in seen:
                        seen.add(child)
                        walk(child)
                elif abs_name not in seen:
                    seen.add(abs_name)
                    o["name"] = abs_name  # make names absolute: folder/file
                    files.append(o)
            if len(page) < limit:
                break
            offset += limit

    walk("")
    return files


def delete_storage_object(bucket: str, name: str) -> None:
    url = f"{bot.SUPABASE_URL}/storage/v1/object/{bucket}/{urllib.parse.quote(name, safe='')}"
    headers = {
        "apikey": bot.SUPABASE_KEY,
        "Authorization": f"Bearer {bot.SUPABASE_KEY}",
    }
    r = httpx.delete(url, headers=headers, timeout=60.0)
    r.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="TikTok video/series URL to re-import after cleanup")
    parser.add_argument("--dry-run", action="store_true", help="only report, delete nothing")
    args = parser.parse_args()

    print("Scanning Supabase storage for 0-byte video files…")
    objects = list_storage_objects(bot.STORAGE_BUCKET)
    broken = [o for o in objects if (o.get("metadata") or {}).get("size", 1) == 0 and _VIDEO_NAME_RE.match(o.get("name", ""))]
    broken = [o for o in broken if o["name"].endswith(".mp4")]

    if not broken:
        print("No 0-byte video files found. Nothing to clean.")
    else:
        print(f"Found {len(broken)} 0-byte video file(s):")
        store = bot.Store()
        for o in broken:
            name = o["name"]
            m = _VIDEO_NAME_RE.match(name)
            folder, num = m.group("folder"), int(m.group("num"))
            print(f"  - {name}")
            if args.dry_run:
                continue
            delete_storage_object(bot.STORAGE_BUCKET, name)
            print(f"    deleted from storage")
            res = store.db.table("episodes").delete().eq("series_id", folder).eq("episode_number", num).execute()
            if getattr(res, "data", None):
                print(f"    deleted episode row (ep {num}) from DB")
            else:
                print(f"    no DB episode row matched")
        # Fix episode_count on affected series (only when actually deleting)
        if not args.dry_run:
            folders = {_VIDEO_NAME_RE.match(o["name"]).group("folder") for o in broken}
            for folder in folders:
                cnt = store.db.table("episodes").select("id", count="exact").eq("series_id", folder).execute().count or 0
                store.db.table("series").update({"episode_count": cnt}).eq("id", folder).execute()
                print(f"series '{folder}': episode_count -> {cnt}")
    if args.url:
        if args.dry_run:
            print("DRY-RUN: skipping re-import")
            return
        print(f"\nRe-importing series from {args.url} (full re-download + overwrite)…")
        asyncio.run(bot._playlist(args.url, None, force=True))
    else:
        print("\nDone. Send the series TikTok URL to the bot to re-import broken episodes.")


if __name__ == "__main__":
    main()
