#!/usr/bin/env python3
"""
One-time TikTok login helper.

Opens a browser, lets you log in to TikTok manually, then saves the
Playwright storage state so the Telegram bot / webhook can reuse it.

Usage::

    python scripts/login_tiktok.py
"""
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from tiktok_playwright import _do_login  # noqa: E402

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)

if __name__ == "__main__":
    import asyncio
    asyncio.run(_do_login())
