"""Build the reduced database that gets published to the site.

Why a separate artefact
-----------------------
The local database keeps everything: every act, every index, so ad-hoc
analysis is fast. The deployed copy cannot. A Vercel function's `/tmp` is
about 512 MB, and the refresh has to hold the new database there, so the
published file has to stay well under that or the site silently stops
updating.

Two reductions, in order of how much they buy:

1. **Recent acts only.** Older acts are a small share of the data and the
   least useful -- agents, maps and metas have changed. Keeping the last
   few acts drops most of the size for almost no analytical loss.

2. **Fewer indexes, built on arrival.** Indexes are not shipped; the
   function creates them after download. They cost 278 MB in the file but
   only ~5s to rebuild, and the positional ones (167 MB, for zone
   selection) are skipped entirely -- zone queries fall back to the main
   index at ~0.6s, which is fine for a drag-and-release interaction and is
   not worth breaking the deployment over.

    python -m app.slim --acts 3
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

from .analytics_db import DEFAULT_PATH, SCHEMA

# Acts to keep in the published copy. Three covers the current act plus
# enough history to compare against, at roughly a quarter of the size.
# All acts. The published size is now governed by the row format, not by
# how much history is dropped -- integer positions took the full dataset
# from 525 MB to 155 MB, so there is no longer a reason to truncate it.
DEFAULT_ACTS = 99

SLIM_PATH = DEFAULT_PATH.with_name("analytics-slim.db")

# Indexes the function builds after downloading. Deliberately excludes the
# positional ones: they are 77% of all index space and only speed up zone
# selection, which is usable without them.
RUNTIME_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_k_main ON kills(map_id, avg_tier, act_id, t_ms)",
    "CREATE INDEX IF NOT EXISTS idx_k_m ON kills(m)",
    "CREATE INDEX IF NOT EXISTS idx_k_util ON kills(map_id, ability_id, avg_tier, act_id)"
    " WHERE dmg_type = 1",
    "CREATE INDEX IF NOT EXISTS idx_k_vpos ON kills(map_id, vx, vy)",
    "CREATE INDEX IF NOT EXISTS idx_k_kpos ON kills(map_id, kx, ky)",
    "CREATE INDEX IF NOT EXISTS idx_k_killer ON kills(killer_pid, map_id)"
    " WHERE killer_pid IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_k_victim ON kills(victim_pid, map_id)"
    " WHERE victim_pid IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_k_agent ON kills(ka_id)",
    "CREATE INDEX IF NOT EXISTS idx_k_weapon ON kills(weapon_id)",
    "CREATE INDEX IF NOT EXISTS idx_k_ability ON kills(ability_id, ka_id)",
    "CREATE INDEX IF NOT EXISTS idx_p_main ON plants(map_id, avg_tier, act_id)",
    "CREATE INDEX IF NOT EXISTS idx_p_m ON plants(m)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_dim_name ON dim(kind, name)",
    "CREATE INDEX IF NOT EXISTS idx_m_map ON matches(map_id, avg_tier)",
)


def _act_sort_key(act: str) -> tuple[int, int]:
    import re

    m = re.match(r"e(\d+)a(\d+)", (act or "").lower())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def recent_act_ids_from(
    conn: sqlite3.Connection, schema: str, keep: int
) -> list[int]:
    """The `keep` most recent acts in `schema`, newest first.

    Acts sort numerically, not lexically: e11a5 is newer than e9a3, which a
    plain string sort gets backwards.
    """
    rows = conn.execute(f"SELECT id, name FROM {schema}.dim WHERE kind = 'act'").fetchall()
    ordered = sorted(rows, key=lambda r: _act_sort_key(r[1]), reverse=True)
    return [r[0] for r in ordered[:keep]]


def build_slim(
    source: Path | None = None,
    target: Path | None = None,
    keep_acts: int = DEFAULT_ACTS,
    verbose: bool = True,
) -> Path:
    source = source or DEFAULT_PATH
    target = target or SLIM_PATH
    target.unlink(missing_ok=True)
    Path(str(target) + "-wal").unlink(missing_ok=True)
    Path(str(target) + "-shm").unlink(missing_ok=True)

    started = time.monotonic()
    conn = sqlite3.connect(target)
    try:
        conn.executescript(SCHEMA)
        # Drop the indexes the schema creates: rows insert faster without
        # them, and the function rebuilds what it needs anyway.
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall():
            conn.execute(f"DROP INDEX IF EXISTS {row[0]}")

        conn.execute("ATTACH ? AS full", (str(source),))
        # Read the act list from the source: this database's own dim table
        # is still empty at this point.
        acts = recent_act_ids_from(conn, "full", keep_acts)
        if not acts:
            raise RuntimeError("no acts found in the source database")
        placeholders = ",".join("?" * len(acts))

        conn.execute("DELETE FROM dim")
        conn.execute("INSERT INTO dim SELECT * FROM full.dim")
        conn.execute(
            f"INSERT INTO matches SELECT * FROM full.matches WHERE act_id IN ({placeholders})",
            acts,
        )
        conn.execute(
            f"INSERT INTO kills SELECT * FROM full.kills WHERE act_id IN ({placeholders})",
            acts,
        )
        conn.execute(
            f"INSERT INTO plants SELECT * FROM full.plants WHERE act_id IN ({placeholders})",
            acts,
        )
        conn.execute("INSERT OR REPLACE INTO meta SELECT * FROM full.meta")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('slim_acts', ?)",
            (str(keep_acts),),
        )
        conn.commit()
        conn.execute("DETACH full")
        conn.commit()

        # Precompute the facet payload into the file, so the deployed site
        # answers /api/facets from one row instead of grouping over every
        # kill -- which took over 7s on the function.
        conn.close()
        from .analytics_db import AnalyticsDB

        AnalyticsDB(target).rebuild_facet_cache()
        conn = sqlite3.connect(target)
        conn.execute("VACUUM")
        conn.commit()

        if verbose:
            names = conn.execute(
                f"SELECT name FROM dim WHERE kind='act' AND id IN ({placeholders})", acts
            ).fetchall()
            matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            kills = conn.execute("SELECT COUNT(*) FROM kills").fetchone()[0]
            print(f"acts kept:   {', '.join(sorted(n[0] for n in names), )}")
            print(f"matches:     {matches:,}")
            print(f"kills:       {kills:,}")
            print(f"size:        {target.stat().st_size / 1e6:.0f} MB")
            print(f"built in:    {time.monotonic() - started:.0f}s")
    finally:
        conn.close()
    return target


def ensure_indexes(path: Path, verbose: bool = False) -> float:
    """Create the query indexes on a downloaded slim database.

    Runs once per cold start. ~5s against 2.9M rows, against 278 MB that
    would otherwise have to fit through /tmp.
    """
    started = time.monotonic()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        for statement in RUNTIME_INDEXES:
            conn.execute(statement)
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()
    elapsed = time.monotonic() - started
    if verbose:
        print(f"[slim] indexes built in {elapsed:.1f}s")
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the published database.")
    parser.add_argument("--acts", type=int, default=DEFAULT_ACTS, help="recent acts to keep")
    parser.add_argument("--source", default=None)
    parser.add_argument("--target", default=None)
    args = parser.parse_args()
    try:
        build_slim(
            Path(args.source) if args.source else None,
            Path(args.target) if args.target else None,
            keep_acts=args.acts,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
