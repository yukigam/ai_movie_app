#!/usr/bin/env python3
"""Localhost series importer — runs the EXACT production pipeline
(extract -> pending rows -> unified parallel resolve+download+upload ->
completeness gate -> final sweep) without Telegram.

Usage:
    python scripts/import_series_local.py <tiktok_url> [--force]

Prints a final EP 1..N completeness table when done.
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

import telegram_bot as bot                # noqa: E402
from tiktok_series import extract_series  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="Import a TikTok drama series locally")
    ap.add_argument("url", help="TikTok video / shortdrama episode URL")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even already-healthy episodes")
    args = ap.parse_args()

    t0 = time.time()
    print(f"[1/4] Extracting series from {args.url} …")
    episodes = await asyncio.to_thread(
        extract_series, args.url, lambda s: print("   .", s[:100]))
    meta = next((e["_meta"] for e in episodes if "_meta" in e), {})
    title = meta.get("series_title") or "Unknown"
    last_ep = meta.get("last_ep_num")
    print(f"      '{title}' — {len(episodes)} episodes"
          + (f" (official total {last_ep})" if last_ep else "")
          + f" in {time.time() - t0:.1f}s")

    skey = bot.slug_id(title)

    print("[2/4] Running production pipeline (register pending -> parallel import)…")
    await bot._playlist_from_episodes(episodes, None, force=args.force)

    # Wait for every detached background import this run spawned.
    bg = [t for t in list(bot._BG_TASKS.values()) if not t.done()]
    if bg:
        print(f"      waiting for {len(bg)} background task(s)…")
        await asyncio.gather(*bg, return_exceptions=True)

    # ── Final completeness console report ──
    print(f"\n[3/4] Verifying database… ({time.time() - t0:.0f}s elapsed)")
    store = bot.Store()
    srow = (store.db.table("series").select("title, poster_url")
            .eq("id", skey).limit(1).execute().data or [{}])[0]
    states = store.episode_states(skey)
    have = await bot._healthy_db_episodes(store, skey, last_ep)

    total = last_ep or (max(states) if states else 0)
    missing = [n for n in range(1, total + 1) if n not in have]

    poster = "YES" if srow.get("poster_url") else "NO"
    print(f"      Series row : '{srow.get('title')}' | poster: {poster}")
    print(f"[4/4] COMPLETENESS: {len(have)}/{total} healthy")
    for n in range(1, total + 1):
        st = states.get(n) or {}
        mark = "OK " if n in have else ("pend" if st.get("status") == "pending" else "MISS")
        print(f"   EP {n:>3} [{mark}] {st.get('status') or 'missing'}")
    if missing:
        print(f"\nMISSING ({len(missing)}): {missing}")
        print("   -> re-run (any episode URL heals pending rows automatically).")
        return 1
    print(f"\nALL {total}/{total} EPISODES PRESENT IN ORDER (EP 1..{total})"
          f" — total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
