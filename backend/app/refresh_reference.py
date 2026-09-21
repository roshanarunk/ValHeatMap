"""Refresh cached Valorant reference data from valorant-api.com.

    python -m app.refresh_reference

Trimmed to the fields the app actually uses so the cache stays small and
diffs stay readable.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REF_DIR = Path(__file__).resolve().parent / "data"
BASE = "https://valorant-api.com/v1"

ENDPOINTS = {
    "maps.json": (
        f"{BASE}/maps",
        (
            "uuid", "displayName", "mapUrl", "displayIcon", "splash", "listViewIcon",
            "xMultiplier", "yMultiplier", "xScalarToAdd", "yScalarToAdd", "callouts",
        ),
    ),
    "agents.json": (
        f"{BASE}/agents?isPlayableCharacter=true",
        ("uuid", "displayName", "role", "displayIcon", "fullPortrait", "bustPortrait", "abilities"),
    ),
    "weapons.json": (
        f"{BASE}/weapons",
        ("uuid", "displayName", "category", "displayIcon", "shopData"),
    ),
}


def _trim(entry: dict, keys: tuple[str, ...]) -> dict:
    out = {k: entry.get(k) for k in keys if k in entry}
    if "role" in out and isinstance(out["role"], dict):
        out["role"] = {"displayName": out["role"].get("displayName", "")}
    if "shopData" in out and isinstance(out["shopData"], dict):
        out["shopData"] = {"categoryText": out["shopData"].get("categoryText", "")}
    if "abilities" in out and isinstance(out["abilities"], list):
        out["abilities"] = [
            {"slot": a.get("slot"), "displayName": a.get("displayName")}
            for a in out["abilities"]
        ]
    if "callouts" in out and isinstance(out["callouts"], list):
        out["callouts"] = [
            {
                "regionName": c.get("regionName"),
                "superRegionName": c.get("superRegionName"),
                "location": c.get("location"),
            }
            for c in (out["callouts"] or [])
        ]
    return out


def main() -> int:
    REF_DIR.mkdir(parents=True, exist_ok=True)
    for filename, (url, keys) in ENDPOINTS.items():
        print(f"fetching {url} ...", flush=True)
        with urllib.request.urlopen(url, timeout=60) as resp:
            payload = json.load(resp)
        if payload.get("status") != 200:
            print(f"  unexpected status {payload.get('status')}", file=sys.stderr)
            return 1
        trimmed = {"status": 200, "data": [_trim(e, keys) for e in payload["data"]]}
        target = REF_DIR / filename
        target.write_text(json.dumps(trimmed, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote {target} ({len(trimmed['data'])} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
