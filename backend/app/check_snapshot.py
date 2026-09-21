"""Verify the published snapshot is reachable, valid and current.

    python -m app.check_snapshot

Run this after publishing, before deploying the site. It checks the things
that actually break:

  * the URL serves the file at all (403/404/522 each mean something
    different, so they are reported differently)
  * the bytes are a gzip stream, not an error page
  * the edge is serving what we just published, not an older cached copy --
    the silent failure, where the origin is correct and a browser download
    looks fine while the deployed function still gets a stale database
  * Cloudflare is caching it, which is the whole reason to use a custom
    domain rather than the rate-limited r2.dev subdomain
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

from .config import load_env
from .publish import OBJECT_KEY
from .snapshot import USER_AGENT, public_base


def _head(url: str) -> tuple[int, dict[str, str]]:
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}
    except urllib.error.URLError as exc:
        print(f"  could not connect: {exc.reason}", file=sys.stderr)
        return 0, {}


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
            print("  -> object missing, or the custom domain is still")
            print("     Initializing. Publish first: python -m app.publish")
        elif status in (401, 403):
            print("  -> the bucket is not reachable publicly. Connect a custom")
            print("     domain (Settings > Custom Domains), or enable the")
            print("     r2.dev subdomain under Settings > Public access.")
        elif status == 522:
            print("  -> a hand-edited DNS record is likely. Remove it and")
            print("     reconnect via Settings > Custom Domains.")
        return 1

    size = int(headers.get("content-length", 0))
    print(f"  HTTP 200, {size / 1e6:.1f} MB")

    # --- is it really a gzip stream? -----------------------------------
    # Range-request two bytes rather than pulling the whole file.
    request = urllib.request.Request(
        url, headers={"Range": "bytes=0-1", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            magic = resp.read(2)
        if magic == b"\x1f\x8b":
            print("  looks like gzip, as expected")
        else:
            print(f"  ! first bytes are {magic!r}, not a gzip header")
            print("    -> the URL is probably serving an error page.")
            return 1
    except urllib.error.HTTPError:
        print("  (range requests unsupported; skipping content check)")

    # --- is the edge serving what we just published? -------------------
    bust = f"{url}{'&' if '?' in url else '?'}v={int(time.time())}"
    _, origin = _head(bust)
    edge_etag = headers.get("etag", "")
    origin_etag = origin.get("etag", "")
    if origin_etag and edge_etag and origin_etag != edge_etag:
        age = headers.get("age", "?")
        print(f"\n  ! the edge is serving an OLDER copy (age {age}s)")
        print(f"    edge   {headers.get('content-length', '?')} bytes  {edge_etag}")
        print(f"    origin {origin.get('content-length', '?')} bytes  {origin_etag}")
        print("    -> purge it: Cloudflare dashboard > Caching > Configuration")
        print("       > Purge Everything. To stop it recurring, add a Cache")
        print("       Rule with a 60s Edge TTL (DEPLOY.md 2.2).")
        return 1
    if origin_etag:
        print("  edge copy matches the origin")

    # --- cached at the edge? -------------------------------------------
    served_by = headers.get("cf-cache-status")
    if served_by is None:
        print("\n  cf-cache-status absent -- not going through Cloudflare's")
        print("  cache. On an r2.dev URL that is expected: every cold start")
        print("  pulls the full file from the origin bucket. A custom domain")
        print("  fixes it (DEPLOY.md 2.2).")
        return 0

    print(f"  cache status: {served_by}")

    ttl = 0
    for part in headers.get("cache-control", "").split(","):
        if "max-age" in part and "=" in part:
            try:
                ttl = int(part.split("=")[1].strip())
            except ValueError:
                ttl = 0
    if ttl:
        print(f"  edge TTL: {ttl}s")
        if ttl > 300:
            print(f"    -> a new publish can take up to {ttl // 60} minutes to")
            print("       reach the site. A Cache Rule with a 60s Edge TTL")
            print("       makes updates land promptly (DEPLOY.md 2.2).")

    print(f"\nUse this in Vercel:\n  VALHEATMAP_SNAPSHOT_URL = {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
