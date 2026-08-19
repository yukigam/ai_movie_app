#!/usr/bin/env python3
"""Offline self-test / import verification — runs the REAL bot pipeline
(extraction → pending-first registration → background import → final
completeness sweep) with console messaging, then verifies against the
DATABASE that the official series name, poster and ALL episodes 1..N
are registered playable.

Usage:
    python scripts/verify_and_import.py <tiktok_video_url>
    python scripts/verify_and_import.py <tiktok_video_url> --expected 50

On success prints:
    ✅ Бүх <N> анги, киноны нэр, poster зураг амжилттай database-д бүртгэгдлээ
and exits 0.  Any verification failure prints the reason and exits 1.
"""
from __future__ import annotations

import asyncio
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass


async def run(url: str, expected: int | None = None) -> int:
    import telegram_bot as bot
    from tiktok_series import extract_series

    if not (bot.SUPABASE_URL and bot.SUPABASE_KEY):
        print("❌ EXPO_PUBLIC_SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing in .env")
        return 1

    def progress(text: str) -> None:
        print(f"  [extract] {text}")

    print(f"🔍 Extract: {url}")
    result = await asyncio.wait_for(
        asyncio.to_thread(extract_series, url, progress),
        timeout=900,
    )
    if not result:
        print("❌ Extraction returned nothing")
        return 1

    meta = result[0].get("_meta") or {}
    n = expected or meta.get("last_ep_num") or (len(result) - 1)
    title = str(meta.get("series_title") or "").strip()
    cover = str(meta.get("series_cover") or "").strip()
    print(f"ℹ️  Series: {title!r} | poster: {'yes' if cover else 'NO'} | official total: {n}")

    if not title:
        print("❌ Series title missing from extraction meta")
        return 1
    if not cover:
        print("❌ Series poster missing from extraction meta")
        return 1
    if not n or n <= 0:
        print("❌ Official episode total missing from extraction meta")
        return 1

    eps = [e for e in result if not e.get("_meta")]
    found = {int(e["episode"]) for e in eps}
    missing_in_extraction = sorted(set(range(1, n + 1)) - found)
    if missing_in_extraction:
        print(f"⚠️  Extraction returned {len(found)}/{n} — missing {missing_in_extraction}; "
              "the import pipeline (top-up + final sweep) must complete them")
    else:
        print(f"✅ Extraction verified: all 1..{n} episodes + name + poster")

    # ── Import via the REAL pipeline (pending-first + background task) ──
    episodes = list(result)
    skey = bot.slug_id(title)
    print(f"📦 Importing '{title}' via the real pipeline (console mode, msg=None)…")
    await bot._playlist_from_episodes(episodes, None)

    task = bot._BG_TASKS.get(skey)
    if task:
        try:
            await asyncio.wait_for(task, timeout=3600)
            print("ℹ️  Background import finished")
        except asyncio.TimeoutError:
            print("⏰ Background import still running after 60 min — verifying anyway")
        except Exception as e:
            print(f"⚠️ Background import raised: {e} (final sweep still runs)")

    # ── FINAL verification against the DATABASE ──
    store = bot.Store()
    have = await bot._healthy_db_episodes(store, skey, n)
    missing_db = sorted(set(range(1, n + 1)) - have)
    if missing_db:
        print(f"❌ DB verification FAILED — missing episodes: {missing_db} "
              f"({len(have)}/{n} healthy)")
        return 1
    rows = store.db.table("series").select("title, poster_url") \
        .eq("id", skey).execute()
    srow = rows.data[0] if (rows and rows.data) else {}
    if not str(srow.get("poster_url") or ""):
        print("❌ DB verification FAILED — series row has no poster_url")
        return 1
    if not str(srow.get("title") or ""):
        print("❌ DB verification FAILED — series row has no title")
        return 1
    print(f"✅ DB verified: title={srow['title']!r}, poster_url set, "
          f"{len(have)}/{n} episodes healthy (1..{n})")

    print(f"\n✅ Бүх {n} анги, киноны нэр, poster зураг амжилттай database-д бүртгэгдлээ")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = args[0] if args else os.getenv("TIKTOK_URL")
    if not url:
        print("Usage: python scripts/verify_and_import.py <tiktok-video-url> [--expected N]")
        return 2
    expected = None
    if "--expected" in sys.argv:
        idx = sys.argv.index("--expected")
        if idx + 1 < len(sys.argv):
            try:
                expected = int(sys.argv[idx + 1])
            except ValueError:
                print("❌ --expected must be an integer")
                return 2
    try:
        return asyncio.run(run(url, expected))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())