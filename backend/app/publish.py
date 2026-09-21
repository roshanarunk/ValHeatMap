"""Publish the analytics database to S3-compatible object storage.

    python -m app.publish

Uploads `data/analytics.db` (gzipped) so the deployed site can fetch it.
Targets Cloudflare R2 by default -- 10 GB free with no egress charges --
but any S3-compatible endpoint works.

Configure in `.env`:

    R2_ACCOUNT_ID=...
    R2_ACCESS_KEY_ID=...
    R2_SECRET_ACCESS_KEY=...
    R2_BUCKET=valheatmap
    R2_PUBLIC_URL=https://pub-xxxx.r2.dev      # bucket's public URL

Signing is implemented directly (AWS SigV4 over HTTPS) so this needs no
boto3; the whole dependency would be there for one PUT.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import os
import sys
import urllib.request
from pathlib import Path

from .analytics_db import DEFAULT_PATH
from .config import load_env
from .quota import QuotaExceeded, check_upload, record_upload
from .snapshot import compress, public_base

OBJECT_KEY = "analytics.db.gz"

# Cloudflare defaults to caching this for 4 hours, which would leave the
# site serving a stale -- and during the WAL bug, corrupt -- database long
# after a fresh publish. A short max-age plus must-revalidate keeps the
# edge honest while still absorbing repeated cold starts.
CACHE_CONTROL = "public, max-age=60, must-revalidate"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _sign(f"AWS4{secret}".encode("utf-8"), date)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def put_object(
    endpoint: str,
    bucket: str,
    key: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str = "auto",
    content_type: str = "application/gzip",
    cache_control: str = CACHE_CONTROL,
) -> None:
    """Minimal SigV4 PUT. Raises on any non-2xx response."""
    host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    url = f"https://{host}/{bucket}/{key}"
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    # Cache-Control has to be signed as well as sent: SigV4 rejects the
    # request if a signed header is missing, and R2 ignores the header if
    # it is sent unsigned. Canonical headers are sorted by name.
    canonical_headers = (
        f"cache-control:{cache_control}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "cache-control;host;x-amz-content-sha256;x-amz-date"
    canonical_request = (
        f"PUT\n/{bucket}/{key}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, "s3"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    request = urllib.request.Request(url, data=body, method="PUT")
    request.add_header("Host", host)
    request.add_header("x-amz-date", amz_date)
    request.add_header("x-amz-content-sha256", payload_hash)
    request.add_header("Content-Type", content_type)
    request.add_header("Cache-Control", cache_control)
    request.add_header(
        "Authorization",
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}",
    )
    with urllib.request.urlopen(request, timeout=600) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"upload failed: HTTP {resp.status}")


def publish(
    db_path: Path | None = None, verbose: bool = True, slim: bool = True
) -> str:
    load_env()
    source = db_path or DEFAULT_PATH
    if not source.exists():
        raise FileNotFoundError(f"{source} does not exist -- run build_analytics first.")

    if slim and db_path is None:
        # The full database no longer fits in a serverless function's /tmp,
        # so publish a reduced copy: recent acts only, and no indexes (the
        # function rebuilds those in a couple of seconds).
        from .slim import build_slim

        if verbose:
            print("building the slim database ...", flush=True)
        source = build_slim(verbose=verbose)

    account = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET", "valheatmap")
    if not (account and access_key and secret_key):
        raise RuntimeError(
            "R2 is not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID and "
            "R2_SECRET_ACCESS_KEY in .env"
        )

    if verbose:
        print(f"compressing {source.name} ({source.stat().st_size / 1e6:.0f} MB) ...", flush=True)
    gz = compress(source)
    size = gz.stat().st_size
    # Cloudflare has no hard spend cap, so refuse anything that would push
    # us past the free tier before the bytes ever leave.
    try:
        state = check_upload(size)
    except QuotaExceeded:
        gz.unlink(missing_ok=True)
        raise

    if verbose:
        print(f"  -> {size / 1e6:.0f} MB gzipped", flush=True)
        print("uploading ...", flush=True)

    put_object(
        endpoint=f"{account}.r2.cloudflarestorage.com",
        bucket=bucket,
        key=OBJECT_KEY,
        body=gz.read_bytes(),
        access_key=access_key,
        secret_key=secret_key,
    )
    gz.unlink(missing_ok=True)
    state = record_upload(size, state)

    public = public_base()
    url = f"{public}/{OBJECT_KEY}" if public else f"(set R2_PUBLIC_URL) /{OBJECT_KEY}"
    if verbose:
        used = state.summary()
        print(f"published: {url}")
        print(
            f"  month-to-date: {used['writes']} uploads "
            f"({used['writes_pct']}% of self-imposed budget), "
            f"{used['uploaded_mb']} MB sent"
        )
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish the analytics database.")
    parser.add_argument("--db", default=None, help="database path")
    args = parser.parse_args()
    try:
        publish(Path(args.db) if args.db else None)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
