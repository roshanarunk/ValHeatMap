"""CLI utility to pack loose raw match JSON files into parted zip archives.

Usage:
    python -m app.archive_raw
    python -m app.archive_raw --chunk-size 1000
    python -m app.archive_raw --dry-run

This reclaims disk space (typically 85-90% reduction) and eliminates flat-directory
inode saturation by bundling loose JSON files into sequence-numbered, independently
extractable zip archives (archive_0001.zip, archive_0002.zip, etc.).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .db import Database, db as default_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack loose match payloads into parted zip archives.")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Number of matches per zip archive part (default: 1000)",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Maximum number of zip archive chunks to create in this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count unarchived matches and estimate space savings without writing zips",
    )
    args = parser.parse_args()

    database: Database = default_db
    raw_dir = database.raw_dir

    print(f"Scanning raw payloads directory: {raw_dir} ...", flush=True)

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT match_id, payload_path FROM matches "
            "WHERE payload_path NOT LIKE '%.zip%' "
            "ORDER BY started_at ASC"
        ).fetchall()

    total_loose = len(rows)
    print(f"Found {total_loose:,} loose match payloads awaiting archival.", flush=True)

    if total_loose == 0:
        print("All matches are already archived in zip parts. Nothing to do.")
        return 0

    if args.dry_run:
        est_parts = (total_loose + args.chunk_size - 1) // args.chunk_size
        print(f"[dry-run] Would create ~{est_parts} zip archives (chunk size = {args.chunk_size}).")
        total_bytes = 0
        for r in rows[:100]:
            p = raw_dir / r["payload_path"]
            if p.is_file():
                total_bytes += p.stat().st_size
        if rows:
            sample_avg = total_bytes / min(len(rows), 100)
            est_total_gb = (sample_avg * total_loose) / 1e9
            print(f"[dry-run] Estimated uncompressed size: ~{est_total_gb:.2f} GB")
            print(f"[dry-run] Estimated compressed size:   ~{est_total_gb * 0.12:.2f} GB")
            print(f"[dry-run] Estimated disk reclaimed:    ~{est_total_gb * 0.88:.2f} GB")
        return 0

    started = time.monotonic()
    print(f"Beginning compression into zip parts ({args.chunk_size} matches per part) ...", flush=True)

    res = database.archive_raw(chunk_size=args.chunk_size, max_chunks=args.max_chunks)

    elapsed = time.monotonic() - started
    archived = res["archived"]
    archives_created = res["archives_created"]
    freed_mb = res["bytes_freed"] / 1e6

    print("\n--- Archiving Complete ---")
    print(f"Matches archived:   {archived:,}")
    print(f"Zip parts created:  {archives_created}")
    print(f"Disk space freed:   {freed_mb:.1f} MB ({freed_mb / 1000:.2f} GB)")
    print(f"Elapsed time:       {elapsed:.1f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
