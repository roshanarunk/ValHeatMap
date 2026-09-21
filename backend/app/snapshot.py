"""Publishing and fetching the analytics database.

The crawler runs locally, where it is fast and free. The deployed site is
read-only and needs a copy of the resulting database. This module moves it:

    publish  local analytics.db  ->  object storage (S3-compatible, e.g. R2)
    ensure   object storage      ->  the function's writable temp dir

R2 is the intended target because its free tier is 10 GB with zero egress
fees, but any S3-compatible endpoint works -- it is all one API.

On a normal local run neither side does anything: `ensure_local_db` just
returns the local path when no remote is configured.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

from .analytics_db import DEFAULT_PATH

# Where a downloaded snapshot lands. /tmp is the only writable path on most
# serverless runtimes, and it survives between warm invocations.
CACHE_DIR = Path(os.environ.get("VALHEATMAP_CACHE_DIR") or tempfile.gettempdir())
CACHED_DB = CACHE_DIR / "valheatmap-analytics.db"
# Written next to the cache so a warm start can tell whether the remote
# copy has changed without re-downloading it.
ETAG_FILE = CACHE_DIR / "valheatmap-analytics.etag"


def public_base() -> str:
    """R2_PUBLIC_URL with a scheme, no trailing slash.

    A bare hostname ("data.example.com") is the natural thing to paste out
    of the Cloudflare dashboard, so accept it rather than failing with a
    urllib "unknown url type" traceback.
    """
    raw = (os.environ.get("R2_PUBLIC_URL") or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw


def snapshot_url() -> str | None:
    """Public URL of the published database, if one is configured."""
    return os.environ.get("VALHEATMAP_SNAPSHOT_URL") or None


def ensure_local_db(force: bool = False) -> Path:
    """Return a path to a usable analytics database.

    Local development just uses the file on disk. When a snapshot URL is
    set (the deployed case), the snapshot is downloaded once per cold start
    and reused while the instance stays warm.
    """
    url = snapshot_url()
    if not url:
        return DEFAULT_PATH

    if CACHED_DB.exists() and not force:
        return CACHED_DB

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHED_DB.with_suffix(".part")
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=120) as resp:
        etag = resp.headers.get("ETag", "")
        compressed = url.endswith(".gz") or resp.headers.get("Content-Encoding") == "gzip"
        with tmp.open("wb") as fh:
            if compressed:
                # Stream through gzip so a 100 MB database never has to be
                # held in memory in full.
                with gzip.GzipFile(fileobj=resp) as gz:
                    shutil.copyfileobj(gz, fh, length=1 << 20)
            else:
                shutil.copyfileobj(resp, fh, length=1 << 20)
    tmp.replace(CACHED_DB)
    if etag:
        ETAG_FILE.write_text(etag, encoding="utf-8")
    return CACHED_DB


def remote_etag() -> str | None:
    """HEAD the snapshot to see whether a newer copy has been published."""
    url = snapshot_url()
    if not url:
        return None
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.headers.get("ETag")
    except OSError:
        return None


def cached_etag() -> str | None:
    if ETAG_FILE.exists():
        try:
            return ETAG_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def refresh_if_stale() -> bool:
    """Re-download when the published snapshot differs. Returns True if it did."""
    if not snapshot_url():
        return False
    remote = remote_etag()
    if remote and remote != cached_etag():
        ensure_local_db(force=True)
        return True
    return False


# --- publishing ---------------------------------------------------------
def compress(source: Path, target: Path | None = None) -> Path:
    """gzip the database for upload. SQLite compresses well (~3x)."""
    target = target or source.with_suffix(".db.gz")
    with source.open("rb") as src, gzip.open(target, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=1 << 20)
    return target


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
