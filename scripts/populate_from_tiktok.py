#!/usr/bin/env python3
"""
Populate Supabase series/episodes table from a TikTok video URL.

Requirements:
    pip install -r scripts/requirements.txt

Usage:
    # Add episode to existing series
    python scripts/populate_from_tiktok.py <tiktok_url> \\
        --series-id <id> --episode <number> [--free]

    # Create new series and add first episode
    python scripts/populate_from_tiktok.py <tiktok_url> \\
        --create --title "My Series" --genre "Sci-Fi" \\
        --description "Description here" [--episode 1]

Examples:
    python scripts/populate_from_tiktok.py \\
        https://www.tiktok.com/@user/video/1234567890 \\
        --series-id series-1 --episode 5 --free

    python scripts/populate_from_tiktok.py \\
        https://www.tiktok.com/@user/video/1234567890 \\
        --create --title "Cyber Love" --genre "Romance" \\
        --description "A story about AI love." --episode 1 --free
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SUPABASE_URL = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
VALID_GENRES = {"Sci-Fi", "Fantasy", "Romance", "Horror"}


def _require_env() -> tuple[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: EXPO_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or EXPO_PUBLIC_SUPABASE_ANON_KEY) must be set in .env")
        sys.exit(1)
    using_service_role = os.getenv("SUPABASE_SERVICE_ROLE_KEY") is not None
    if not using_service_role:
        print("WARNING: Using anon key — INSERT may fail if RLS policies don't allow it.")
        print("  Add SUPABASE_SERVICE_ROLE_KEY to .env (from Supabase Dashboard > Project Settings > API) for full access.")
    return SUPABASE_URL, SUPABASE_KEY


def init_supabase() -> Client:
    url, key = _require_env()
    return create_client(url, key)


def extract_tiktok(url: str) -> dict:
    try:
        import yt_dlp
    except ImportError:
        print("ERROR: yt-dlp is not installed. Run: pip install -r scripts/requirements.txt")
        sys.exit(1)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "format": "bestvideo+bestaudio/best",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"ERROR: Failed to extract TikTok info: {e}")
        sys.exit(1)

    video_url = info.get("url") or ""
    if not video_url and info.get("formats"):
        best = max(info["formats"], key=lambda f: f.get("height", 0) or 0)
        video_url = best.get("url") or ""

    title = (info.get("title") or "TikTok Video").strip()
    # Remove auto-uploader suffix like "♬ original sound - User"
    title = re.sub(r"\s*[—\-–]\s*(♬.*|original sound.*|sonido original.*)$", "", title).strip()
    if not title:
        title = "Untitled"

    description = (info.get("description") or title).strip()
    thumbnail = info.get("thumbnail") or ""
    duration_sec = info.get("duration") or 30
    duration_min = max(1, round(duration_sec / 60))

    return {
        "title": title,
        "description": description[:500],
        "video_url": video_url,
        "thumbnail_url": thumbnail,
        "duration": duration_min,
    }


def slug_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "series"


def series_exists(supabase: Client, series_id: str) -> bool:
    result = supabase.table("series").select("id", count="exact").eq("id", series_id).execute()
    return result.count is not None and result.count > 0


def create_series(supabase: Client, series_id: str, title: str, genre: str, description: str) -> None:
    if genre not in VALID_GENRES:
        print(f"ERROR: Invalid genre '{genre}'. Choose from: {', '.join(sorted(VALID_GENRES))}")
        sys.exit(1)
    payload = {
        "id": series_id,
        "title": title,
        "genre": genre,
        "description": description,
        "poster_url": "",
        "banner_url": "",
        "play_count": 0,
        "episode_count": 0,
    }
    supabase.table("series").insert(payload).execute()
    print(f"  Created series '{title}' (id: {series_id})")


def current_episode_count(supabase: Client, series_id: str) -> int:
    result = supabase.table("episodes").select("id", count="exact").eq("series_id", series_id).execute()
    count = result.count if result.count is not None else 0
    if count is None:
        count = 0
    return count


def insert_episode(
    supabase: Client,
    series_id: str,
    episode_number: int,
    data: dict,
    is_free: bool,
) -> str:
    episode_id = f"ep-{series_id}-{episode_number}"
    payload = {
        "id": episode_id,
        "series_id": series_id,
        "episode_number": episode_number,
        "title": data["title"],
        "description": data["description"],
        "video_url": data["video_url"],
        "thumbnail_url": data["thumbnail_url"],
        "duration": data["duration"],
        "is_free": is_free,
    }
    supabase.table("episodes").insert(payload).execute()

    # Update series episode_count
    ep_count = current_episode_count(supabase, series_id)
    supabase.table("series").update({"episode_count": ep_count}).eq("id", series_id).execute()

    return episode_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate Supabase with a TikTok short video as a series episode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("tiktok_url", help="TikTok video URL")
    parser.add_argument("--series-id", help="Existing series ID to attach episode to")
    parser.add_argument("--episode", type=int, default=None, help="Episode number")
    parser.add_argument("--free", action="store_true", help="Mark episode as free (default: locked)")
    parser.add_argument("--create", action="store_true", help="Create a new series")
    parser.add_argument("--title", help="Title for new series (required with --create)")
    parser.add_argument("--genre", help=f"Genre for new series: {', '.join(sorted(VALID_GENRES))}")
    parser.add_argument("--description", help="Description for new series")
    args = parser.parse_args()

    if args.create and not args.title:
        print("ERROR: --title is required when --create is specified")
        sys.exit(1)

    if not args.create and not args.series_id:
        print("ERROR: Either --series-id (existing) or --create (new) must be specified")
        sys.exit(1)

    print(f"[1/3] Extracting video info from TikTok...")
    data = extract_tiktok(args.tiktok_url)
    print(f"  Title:      {data['title']}")
    print(f"  Duration:   {data['duration']} min")
    print(f"  Thumbnail:  {data['thumbnail_url'][:60]}...")

    supabase = init_supabase()
    series_id = args.series_id

    if args.create:
        series_id = series_id or slug_id(args.title)
        print(f"[2/3] Creating new series '{args.title}' (id: {series_id})...")
        create_series(supabase, series_id, args.title, args.genre or "Sci-Fi", args.description or data["description"])
    else:
        if not series_exists(supabase, series_id):
            print(f"ERROR: Series '{series_id}' not found in database. Use --create to create it.")
            sys.exit(1)
        print(f"[2/3] Using existing series '{series_id}'...")

    ep_num = args.episode
    if ep_num is None:
        ep_num = current_episode_count(supabase, series_id) + 1

    print(f"[3/3] Inserting episode {ep_num}...")
    ep_id = insert_episode(supabase, series_id, ep_num, data, args.free)
    print(f"  Done! Episode '{data['title']}' inserted as {ep_id}")
    print(f"  Video URL: {data['video_url'][:80]}...")


if __name__ == "__main__":
    main()
