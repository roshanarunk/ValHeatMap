"""Backfill new tactical flags (support, crossfire, advantage, clutch, low impact) into kills.

Usage:
    python -m app.backfill_tactical_flags --tracked-only
    python -m app.backfill_tactical_flags --limit 100
"""

from __future__ import annotations

import argparse
import sys
import time

from .analytics_db import (
    FLAG_ADVANTAGE_DEATH,
    FLAG_CLUTCH_KILL,
    FLAG_CROSSFIRE,
    FLAG_FIRST_BLOOD,
    FLAG_ISOLATED,
    FLAG_LOW_IMPACT,
    FLAG_POST_PLANT,
    FLAG_ROUND_WON,
    FLAG_SUPPORTED,
    FLAG_TRADE_KILL,
    FLAG_TRADED,
    AnalyticsDB,
)
from .analytics.kills import enrich
from .db import db as raw_db
from .store import parse_any


def backfill_match_flags(db: AnalyticsDB, conn, m: int, match_id: str) -> int:
    payload = raw_db.get_payload(match_id)
    if payload is None:
        return 0
    try:
        match = parse_any(payload)
    except (ValueError, KeyError):
        return 0

    enriched = enrich(match)
    updates = []
    for ek in enriched:
        k = ek.kill
        flags = (
            (FLAG_TRADED if ek.traded else 0)
            | (FLAG_TRADE_KILL if ek.trade_kill else 0)
            | (FLAG_FIRST_BLOOD if ek.first_blood else 0)
            | (FLAG_POST_PLANT if ek.post_plant else 0)
            | (FLAG_ROUND_WON if ek.round_won else 0)
            | (FLAG_SUPPORTED if ek.supported else 0)
            | (FLAG_ISOLATED if ek.isolated else 0)
            | (FLAG_CROSSFIRE if ek.crossfire else 0)
            | (FLAG_ADVANTAGE_DEATH if ek.advantage_death else 0)
            | (FLAG_CLUTCH_KILL if ek.clutch_kill else 0)
            | (FLAG_LOW_IMPACT if ek.low_impact else 0)
        )
        updates.append((flags, m, k.round_num, k.time_in_round_ms))

    if not updates:
        return 0

    cur = conn.executemany(
        """UPDATE kills SET flags = ?
           WHERE m = ? AND round_num = ? AND t_ms = ?""",
        updates,
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def run(
    db: AnalyticsDB | None = None,
    tracked_only: bool = True,
    limit: int | None = None,
    verbose: bool = True,
) -> dict[str, int]:
    db = db or AnalyticsDB()
    started = time.monotonic()

    with db.connect() as conn:
        if tracked_only:
            # Matches where any tracked player participated
            sql = """
                SELECT DISTINCT m.id, m.match_id
                FROM matches m
                JOIN kills k ON k.m = m.id
                JOIN tracked_players tp ON (tp.pid = k.killer_pid OR tp.pid = k.victim_pid)
                ORDER BY m.started_at DESC
            """
        else:
            sql = "SELECT id, match_id FROM matches ORDER BY started_at DESC"

        if limit:
            sql += f" LIMIT {int(limit)}"

        todo = [(r["id"], r["match_id"]) for r in conn.execute(sql)]

    if verbose:
        print(f"Backfilling tactical flags for {len(todo)} matches...", file=sys.stderr)

    updated_matches = 0
    updated_kills = 0

    with db.connect() as conn:
        for m, match_id in todo:
            n = backfill_match_flags(db, conn, m, match_id)
            if n > 0:
                updated_matches += 1
                updated_kills += n

    elapsed = round(time.monotonic() - started, 2)
    if verbose:
        print(
            f"Done: {updated_kills} kills across {updated_matches} matches updated in {elapsed}s.",
            file=sys.stderr,
        )

    return {"matches": updated_matches, "kills": updated_kills}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Process all matches instead of only tracked players")
    parser.add_argument("--limit", type=int, help="Maximum number of matches to process")
    args = parser.parse_args()

    run(tracked_only=not args.all, limit=args.limit)
