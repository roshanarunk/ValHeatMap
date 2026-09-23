"""Build the analytics database from stored raw payloads.

    python -m app.build_analytics              # incremental
    python -m app.build_analytics --rebuild    # from scratch

Incremental by default: matches already present are skipped, so this is
cheap to run after every crawl. A full rebuild is only needed when the
analytics change (a new stat, a parser fix), and that is the whole reason
the raw payloads are kept.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .analytics.kills import enrich
from .analytics_db import AnalyticsDB
from .db import db as raw_db
from .reference import get_map
from .store import parse_any


def build(
    analytics: AnalyticsDB,
    rebuild: bool = False,
    limit: int | None = None,
    verbose: bool = True,
) -> dict[str, int]:
    if rebuild:
        with analytics.connect() as conn:
            conn.executescript(
                "DELETE FROM kills; DELETE FROM plants; DELETE FROM matches;"
            )

    stored = skipped = failed = kills = 0
    started = time.monotonic()
    total_est = raw_db.match_count()
    if limit:
        total_est = min(total_est, limit)

    for i, (match_id, payload) in enumerate(raw_db.iter_payloads(limit=limit)):
        if not rebuild and analytics.has_match(match_id):
            skipped += 1
            continue
        try:
            match = parse_any(payload)
            if not match.meta.match_id:
                match.meta.match_id = match_id
            map_info = get_map(match.meta.map_id) or get_map(match.meta.map_name)
            n = analytics.add_match(match, map_info, enrich(match))
            if n:
                stored += 1
                kills += n
            else:
                skipped += 1
        except (json.JSONDecodeError, ValueError, KeyError, TypeError, OSError):
            failed += 1

        if verbose and (i + 1) % 250 == 0:
            rate = (i + 1) / max(0.001, time.monotonic() - started)
            print(
                f"  {i + 1}/{total_est}  stored={stored} skipped={skipped} "
                f"failed={failed}  ({rate:.0f}/s)",
                flush=True,
            )

    processed = stored + skipped + failed
    analytics.set_meta("generated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    analytics.set_meta("source_matches", str(processed))
    return {
        "stored": stored,
        "skipped": skipped,
        "failed": failed,
        "kills": kills,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the analytics database.")
    parser.add_argument("--rebuild", action="store_true", help="discard and rebuild")
    parser.add_argument("--limit", type=int, default=None, help="only N payloads")
    parser.add_argument("--out", default=None, help="output database path")
    args = parser.parse_args()

    analytics = AnalyticsDB(Path(args.out) if args.out else None)
    print(f"building {analytics.path} ...", flush=True)
    result = build(analytics, rebuild=args.rebuild, limit=args.limit)
    for key, value in result.items():
        print(f"{key:>12}: {value}")

    stats = analytics.stats()
    size_mb = analytics.path.stat().st_size / 1e6 if analytics.path.exists() else 0
    print(f"{'db matches':>12}: {stats['matches']:,}")
    print(f"{'db kills':>12}: {stats['kills']:,}")
    print(f"{'db plants':>12}: {stats['plants']:,}")
    print(f"{'file size':>12}: {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
