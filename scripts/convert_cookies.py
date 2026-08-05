#!/usr/bin/env python3
"""
Convert browser-exported TikTok cookies (JSON format) into Playwright's
``storageState`` JSON format and write it to ``playwright_storage.json``.

Usage
-----
    python scripts/convert_cookies.py < input_cookies.json

The input JSON must be an array of cookie objects, each with at minimum:
    domain, name, value, path, secure, httpOnly

Optional fields that are mapped:
    expirationDate  →  expires  (float -> int)
    sameSite        →  mapped to Playwright convention
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT_ROOT / "playwright_storage.json"

# ── SameSite mapping ────────────────────────────────────────────────────────
_SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
    "": "Lax",
}


def _map_samesite(raw: str) -> str:
    return _SAMESITE_MAP.get(raw.strip().lower(), "Lax")


def convert(raw_cookies: list[dict]) -> dict:
    """Convert an array of browser cookie dicts to Playwright storageState."""
    playwright_cookies = []

    for c in raw_cookies:
        domain = c.get("domain", "")
        name = c.get("name", "")
        value = c.get("value", "")

        # Skip entries without name/value
        if not name:
            continue

        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path", "/"),
            "expires": -1,
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": _map_samesite(c.get("sameSite", "")),
        }

        # Map expirationDate → expires (drop fractional part)
        exp = c.get("expirationDate")
        if exp is not None:
            cookie["expires"] = int(exp)

        playwright_cookies.append(cookie)

    return {
        "cookies": playwright_cookies,
        "origins": [],
    }


def main():
    raw = json.load(sys.stdin)
    if not isinstance(raw, list):
        print("ERROR: Input must be a JSON array of cookie objects.", file=sys.stderr)
        sys.exit(1)

    state = convert(raw)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    count = len(state["cookies"])
    print(f"[OK] Converted {count} cookies to {OUTPUT}")


if __name__ == "__main__":
    main()
