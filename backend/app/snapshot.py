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
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .analytics_db import DEFAULT_PATH

# Where a downloaded snapshot lands. /tmp is the only writable path on most
# serverless runtimes, and it survives between warm invocations.
# Cloudflare's bot protection rejects urllib's default "Python-urllib/3.x"
# user-agent with a 403, even on a public bucket. Identify ourselves
# properly so the deployed site can actually fetch its own database.
USER_AGENT = "ValHeatMap/2.0 (+https://github.com/valheatmap)"

CACHE_DIR = Path(os.environ.get("VALHEATMAP_CACHE_DIR") or tempfile.gettempdir())
CACHED_DB = CACHE_DIR / "valheatmap-analytics.db"
# Written next to the cache so a warm start can tell whether the remote
# copy has changed without re-downloading it.
ETAG_FILE = CACHE_DIR / "valheatmap-analytics.etag"


def _request(url: str, **kwargs) -> urllib.request.Request:
    """A Request that always carries our user-agent."""
    headers = dict(kwargs.pop("headers", {}))
    headers.setdefault("User-Agent", USER_AGENT)
    return urllib.request.Request(url, headers=headers, **kwargs)


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
    tmp.unlink(missing_ok=True)

    # /tmp is typically 512 MB on a serverless host, and the database is
    # already most of that. Writing the new copy beside the old one runs
    # the disk out, and the refresh fails silently every time. Removing the
    # old copy first is safe because a failed download falls back to
    # DEFAULT_PATH, and the next request retries.
    if force and CACHED_DB.exists():
        needed = CACHED_DB.stat().st_size
        if _free_space(CACHE_DIR) < needed * 1.15:
            print(
                f"[snapshot] freeing {needed / 1e6:.0f} MB before refresh "
                f"({_free_space(CACHE_DIR) / 1e6:.0f} MB free)"
            )
            CACHED_DB.unlink(missing_ok=True)
            ETAG_FILE.unlink(missing_ok=True)
    # A forced refresh follows an ETag check that bypassed the cache, so the
    # download has to bypass it too. Otherwise the edge can hand back the
    # very copy we just decided was stale, and the instance would keep
    # re-downloading the same old file every poll.
    fetch_url = url
    if force:
        separator = "&" if "?" in url else "?"
        fetch_url = f"{url}{separator}_={int(time.time())}"
    request = _request(
        fetch_url,
        headers={
            "Accept-Encoding": "identity",
            **({"Cache-Control": "no-cache"} if force else {}),
        },
    )
    try:
        return _download(request, tmp, fetch_url)
    except (OSError, urllib.error.URLError, gzip.BadGzipFile) as exc:
        tmp.unlink(missing_ok=True)
        # A download failure at import time must not take the whole app
        # down: an instance with a previously cached copy should keep
        # serving it, and a cold one should start and report the problem
        # through /api/health rather than 500 on every route.
        print(f"[snapshot] could not fetch {url}: {exc}")
        if CACHED_DB.exists():
            print("[snapshot] falling back to the cached copy")
            return CACHED_DB
        return DEFAULT_PATH


def _download(request: urllib.request.Request, tmp: Path, url: str) -> Path:
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
    # The published copy ships without indexes to stay inside /tmp; build
    # them now, once, rather than shipping 278 MB of them.
    try:
        from .slim import ensure_indexes

        ensure_indexes(CACHED_DB, verbose=True)
    except Exception as exc:
        print(f"[snapshot] index build failed, queries will be slow: {exc}")
    if etag:
        ETAG_FILE.write_text(etag, encoding="utf-8")
    return CACHED_DB


def _free_space(path: Path) -> int:
    """Bytes available where the snapshot is cached."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def remote_etag() -> str | None:
    """HEAD the snapshot to see whether a newer copy has been published.

    The request deliberately bypasses the CDN cache. A plain HEAD is served
    from the same edge cache as the file itself, so for the length of the
    edge TTL after a publish it returns the *old* ETag -- the poller
    concludes nothing changed and then waits out its whole interval before
    looking again. Asking the origin directly makes the check authoritative
    and costs one small HEAD per poll.
    """
    url = snapshot_url()
    if not url:
        return None
    separator = "&" if "?" in url else "?"
    request = _request(
        f"{url}{separator}_={int(time.time())}",
        method="HEAD",
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
    )
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
def consistent_copy(source: Path, target: Path) -> Path:
    """Copy a live SQLite database safely.

    Copying the file byte-for-byte is wrong while anything is writing to
    it. In WAL mode recent commits live in `-wal` until a checkpoint, so a
    plain copy captures main-file pages that reference WAL content the copy
    does not include -- the result opens fine and then fails with "database
    disk image is malformed" on the first real query.

    sqlite3's backup API takes a transactionally consistent snapshot
    instead, including anything still in the WAL, without stopping the
    writer. That matters here because the crawler publishes while it runs.
    """
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def compress(source: Path, target: Path | None = None) -> Path:
    """Snapshot the database consistently, then gzip it for upload.

    SQLite compresses about 3x.
    """
    target = target or source.with_suffix(".db.gz")
    staging = source.with_suffix(".snapshot.tmp")
    try:
        consistent_copy(source, staging)
        with staging.open("rb") as src, gzip.open(target, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
    finally:
        staging.unlink(missing_ok=True)
    return target


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
