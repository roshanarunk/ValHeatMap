"""Backfill macro-rotation transitions into analytics.db.

Usage:
    python -m app.backfill_rotations --tracked-only
    python -m app.backfill_rotations --limit 100
    python -m app.backfill_rotations --map Ascent
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from .analytics.rotations import extract_rotations
from .analytics_db import AnalyticsDB
from .db import db as raw_db
from .reference import get_map
from .store import parse_any


def backfill_match_rotations(db: AnalyticsDB, conn, m: int, match_id: str, map_name: str) -> int:
    map_info = get_map(map_name)
    if not map_info or not map_info.callouts:
        return 0

    payload = raw_db.get_payload(match_id)
    if payload is None:
        return 0
    try:
        match = parse_any(payload)
    except (ValueError, KeyError):
        return 0

    map_id = db._dim_id(conn, "map", map_name)
    if map_id is None:
        return 0

    extracted = extract_rotations(match, map_info)
    if not extracted:
        return 0

    # Clean existing
    conn.execute("DELETE FROM rotations WHERE m = ?", (m,))

    rows = []
    for er in extracted:
        pid = db._dim_id(conn, "player", er.player_puuid or None)
        agent_id = db._dim_id(conn, "agent", er.agent or None)
        rows.append(
            (
                m,
                map_id,
                er.round_num,
                er.side,
                pid,
                agent_id,
                er.team or None,
                er.from_zone,
                er.to_zone,
                er.t_start_ms,
                er.t_end_ms,
                er.won,
            )
        )

    conn.executemany(
        """INSERT INTO rotations (
               m, map_id, round_num, side, player_pid, agent_id, team,
               from_zone, to_zone, t_start_ms, t_end_ms, won
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def run(
    db: AnalyticsDB | None = None,
    tracked_only: bool = True,
    map_name: str | None = None,
    limit: int | None = None,
    verbose: bool = True,
) -> dict[str, int]:
    db = db or AnalyticsDB()
    started = time.monotonic()

    with db.connect() as conn:
        if tracked_only:
            sql = """
                SELECT DISTINCT m.id, m.match_id, d.name as map_name
                FROM matches m
                JOIN dim d ON (d.kind = 'map' AND d.id = m.map_id)
                JOIN kills k ON k.m = m.id
                JOIN tracked_players tp ON (tp.pid = k.killer_pid OR tp.pid = k.victim_pid)
                WHERE 1=1
            """
        else:
            sql = """
                SELECT m.id, m.match_id, d.name as map_name
                FROM matches m
                JOIN dim d ON (d.kind = 'map' AND d.id = m.map_id)
                WHERE 1=1
            """

        params: list[Any] = []
        if map_name:
            sql += " AND LOWER(d.name) = LOWER(?)"
            params.append(map_name)

        sql += " ORDER BY m.started_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"

        todo = [(r["id"], r["match_id"], r["map_name"]) for r in conn.execute(sql, params)]

    if verbose:
        print(f"Backfilling macro rotations for {len(todo)} matches...", file=sys.stderr)

    updated_matches = 0
    updated_rotations = 0

    with db.connect() as conn:
        for m, match_id, map_n in todo:
            n = backfill_match_rotations(db, conn, m, match_id, map_n)
            if n > 0:
                updated_matches += 1
                updated_rotations += n

    elapsed = round(time.monotonic() - started, 2)
    if verbose:
        print(
            f"Done: {updated_rotations} rotations across {updated_matches} matches backfilled in {elapsed}s.",
            file=sys.stderr,
        )

    return {"matches": updated_matches, "rotations": updated_rotations}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all", action="store_true", help="Process all matches instead of only tracked players"
    )
    parser.add_argument("--map", type=str, help="Filter by map name")
    parser.add_argument("--limit", type=int, help="Maximum number of matches to process")
    args = parser.parse_args()

    run(tracked_only=not args.all, map_name=args.map, limit=args.limit)
