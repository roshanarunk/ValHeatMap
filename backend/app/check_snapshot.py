"""Verify the published snapshot is reachable and cacheable.

    python -m app.check_snapshot

Run this after connecting a custom domain, before deploying the site. It
checks the things that actually break: that the URL serves the file, that
it is the snapshot we published, and -- the reason to use a custom domain
at all -- that Cloudflare is caching it at the edge rather than pulling
~37 MB from the origin bucket on every cold start.
"""

from __future__ import annotations

import gzip
import os
import sys
import time
import urllib.error
import urllib.request

from .config import load_env
from .publish import OBJECT_KEY
from .snapshot import public_base


def _head(url: str) -> tuple[int, dict[str, str]]:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}


def main() -> int:
    load_env()
    base = public_base()
    if not base:
        print("R2_PUBLIC_URL is not set in .env", file=sys.stderr)
        return 1
    url = f"{base}/{OBJECT_KEY}"
    print(f"checking {url}\n")

    # --- reachable? ----------------------------------------------------
    status, headers = _head(url)
    if status != 200:
        print(f"  HTTP {status}")
        if status == 404:
            print("  -> object missing, or the custom domain is still Initializing.")
            print("     Publish first: python -m app.publish")
        elif status in (401, 403):
            print("  -> the bucket is not public. Connect a custom domain, or")
            print("     enable the r2.dev subdomain under Settings > Public access.")
        else:
            print("  -> if this is 522, a hand-edited DNS record is likely;")
            print("     remove it and reconnect via Settings > Custom Domains.")
        return 1

    size = int(headers.get("content-length", 0))
    print(f"  HTTP 200, {size / 1e6:.1f} MB")

    # --- is it really our snapshot? ------------------------------------
    # Range-request the gzip header rather than pulling the whole file.
    request = urllib.request.Request(url, headers={"Range": "bytes=0-1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            magic = resp.read(2)
        if magic == b"\x1f\x8b":
            print("  looks like gzip, as expected")
        else:
            print(f"  ! unexpected first bytes {magic!r} -- not a gzip file")
            return 1
    except urllib.error.HTTPError:
        print("  (range request unsupported; skipping content check)")

    # --- cached at the edge? -------------------------------------------
    # Two HEADs: the first may MISS and populate the cache, the second
    # should HIT. That is the whole benefit of a custom domain.
    served_by = headers.get("cf-cache-status")
    if served_by is None:
        print("\n  cf-cache-status absent -- this is not going through")
        print("  Cloudflare's cache. On an r2.dev URL that is expected:")
        print("  every cold start will pull the full file from the origin.")
        print("  A custom domain fixes it (DEPLOY.md 2.2).")
        return 0

    print(f"\n  cache status: {served_by}")
    if served_by.upper() in {"MISS", "EXPIRED", "BYPASS"}:
        time.sleep(2)
        _, again = _head(url)
        second = again.get("cf-cache-status", "?")
        print(f"  second request: {second}")
        if second.upper() == "HIT":
            print("  -> edge caching is working")
        else:
            print("  -> still not cached. Large objects are not always held;")
            print("     a Cache Rule with an explicit Edge TTL helps (DEPLOY.md 2.2).")
    else:
        print("  -> edge caching is working")

    print(f"\nUse this in Vercel:\n  VALHEATMAP_SNAPSHOT_URL = {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
