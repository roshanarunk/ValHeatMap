"""Fill in player attribution for matches crawled before it was stored.

    python -m app.backfill_players            # everything missing
    python -m app.backfill_players --limit 100

The kills table originally kept agent ids but not who played them, which
makes "show me my kills" unanswerable -- two Jett players in one match are
indistinguishable. The puuids were never lost, only unindexed: they are in
the raw payloads on disk, so this re-reads those and fills the columns in.

Why not just rebuild
--------------------
`build_analytics --rebuild` would also produce the right answer, but it
re-derives every kill, plant, trade window and coordinate transform for
33,000 matches. This only needs two columns, so it updates them in place
and leaves the rest of each row untouched -- about six times faster, and
it never leaves the table empty partway through.

Matches are matched up by (round, time, agents) rather than row id,
because kills have no primary key and their insertion order is not
something to depend on.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from .analytics_db import AnalyticsDB
from .db import db as raw_db
from .store import parse_any

# Matches per transaction. Large enough that commits are not the
# bottleneck, small enough that a crash loses seconds of work.
BATCH = 250


def _payload_for(match_id: str) -> dict | None:
    """The stored raw payload for a match, if we still have it."""
    path = raw_db.raw_dir / f"{match_id}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def pending_matches(conn: sqlite3.Connection, limit: int | None = None) -> list[tuple[int, str]]:
    """Matches that still have unattributed kills, newest first.

    Newest first because a player's recent games are what they look at,
    so a partial backfill is still immediately useful.
    """
    sql = """
        SELECT m.id, m.match_id
        FROM matches m
        WHERE EXISTS (
            SELECT 1 FROM kills k
            WHERE k.m = m.id AND k.killer_pid IS NULL AND k.victim_pid IS NULL
        )
        ORDER BY m.started_at DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r["id"], r["match_id"]) for r in conn.execute(sql)]


def backfill_match(
    db: AnalyticsDB, conn: sqlite3.Connection, m: int, match_id: str
) -> int:
    """Attribute one match's kills. Returns rows updated."""
    payload = _payload_for(match_id)
    if payload is None:
        return 0
    try:
        match = parse_any(payload)
    except (ValueError, KeyError):
        return 0

    # Agent per player, to key rows the same way they were stored.
    agent_of = {p.puuid: p.agent for p in match.players}

    updates = []
    for rnd in match.rounds:
        for k in rnd.kills:
            if not k.killer_puuid or not k.victim_puuid:
                continue
            killer_pid = db._dim_id(conn, "player", k.killer_puuid)
            victim_pid = db._dim_id(conn, "player", k.victim_puuid)
            ka = db._dim_id(conn, "agent", agent_of.get(k.killer_puuid) or None)
            va = db._dim_id(conn, "agent", agent_of.get(k.victim_puuid) or None)
            updates.append(
                (killer_pid, victim_pid, m, k.round_num, k.time_in_round_ms, ka, va)
            )

    if not updates:
        return 0

    # Keyed on the fields that identify a kill within a match. Two kills
    # can share a millisecond in different rounds, but not the same round,
    # time and both agents.
    cur = conn.executemany(
        """UPDATE kills SET killer_pid = ?, victim_pid = ?
           WHERE m = ? AND round_num = ? AND t_ms = ?
             AND ka_id IS ? AND va_id IS ?
             AND killer_pid IS NULL""",
        updates,
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def run(
    db: AnalyticsDB | None = None,
    limit: int | None = None,
    verbose: bool = True,
) -> dict[str, int]:
    db = db or AnalyticsDB()
    started = time.monotonic()

    with db.connect() as conn:
        todo = pending_matches(conn, limit)

    if verbose:
        print(f"{len(todo):,} matches need attribution")

    done = missing = rows = 0
    # Batched rather than one transaction per match: committing 32,000
    # times is slow, and a single transaction over the whole run would
    # discard everything on a crash. A batch also keeps the dim-id cache
    # consistent with what is committed.
    for start in range(0, len(todo), BATCH):
        chunk = todo[start : start + BATCH]
        with db.connect() as conn:
            for m, match_id in chunk:
                updated = backfill_match(db, conn, m, match_id)
                if updated:
                    done += 1
                    rows += updated
                else:
                    missing += 1

        i = start + len(chunk)
        if verbose:
            rate = i / max(time.monotonic() - started, 0.001)
            left = (len(todo) - i) / rate if rate else 0
            print(
                f"  {i:,}/{len(todo):,}  {rows:,} kills attributed"
                f"  {rate:.0f}/s  ~{left / 60:.0f} min left"
            )

    elapsed = time.monotonic() - started
    if verbose:
        print(f"\nattributed {rows:,} kills across {done:,} matches in {elapsed / 60:.1f} min")
        if missing:
            print(f"{missing:,} matches skipped (payload missing or unparseable)")
    return {"matches": done, "kills": rows, "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="only this many matches")
    parser.add_argument("--db", type=Path, help="analytics database path")
    args = parser.parse_args()

    db = AnalyticsDB(args.db) if args.db else AnalyticsDB()
    result = run(db, limit=args.limit)
    if result["kills"]:
        print("\nRebuilding the facet cache...")
        db.rebuild_facet_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
