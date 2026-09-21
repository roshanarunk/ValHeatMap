"""Persistent match storage: SQLite index + raw JSON payloads on disk.

Design
------
The raw upstream payload is the source of truth and is written to
`data/raw/<match_id>.json`. SQLite holds an index of what we have, plus the
crawler's frontier (players still to visit). Nothing derived is stored.

Keeping the raw payloads means an analytics change -- a new stat, a fixed
parser -- is a re-read of local files rather than thousands of API calls we
have already paid for in rate limit.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
RAW_DIR = DATA_ROOT / "raw"
DB_PATH = DATA_ROOT / "valheatmap.db"

# Makes temp filenames unique within a process; the pid separates processes.
_tmp_counter = itertools.count()

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id      TEXT PRIMARY KEY,
    map_name      TEXT NOT NULL,
    mode          TEXT NOT NULL,
    queue         TEXT,
    region        TEXT,
    started_at    INTEGER,
    rounds        INTEGER,
    kills         INTEGER,
    plants        INTEGER,
    source        TEXT,
    payload_path  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_map  ON matches(map_name);
CREATE INDEX IF NOT EXISTS idx_matches_mode ON matches(mode);
CREATE INDEX IF NOT EXISTS idx_matches_time ON matches(started_at DESC);

-- Every player we have seen, and whether we have crawled their history.
CREATE TABLE IF NOT EXISTS players (
    puuid        TEXT PRIMARY KEY,
    name         TEXT,
    tag          TEXT,
    region       TEXT,
    tier         INTEGER DEFAULT 0,
    crawled_at   TEXT,
    match_count  INTEGER DEFAULT 0,
    discovered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_pending ON players(crawled_at, tier DESC);

-- Match ids seen in a matchlist but not yet fetched, and ones that failed
-- permanently, so a re-run does not keep retrying them.
CREATE TABLE IF NOT EXISTS match_queue (
    match_id   TEXT PRIMARY KEY,
    region     TEXT,
    state      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    queued_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_state ON match_queue(state);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | None = None, raw_dir: Path | None = None) -> None:
        self.path = path or DB_PATH
        self.raw_dir = raw_dir or RAW_DIR
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            # WAL lets the crawler write while the API reads.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # --- matches -------------------------------------------------------
    def has_match(self, match_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
            ).fetchone()
        return row is not None

    def save_match(self, match_id: str, payload: dict[str, Any], summary: dict[str, Any]) -> Path:
        """Write the raw payload and index it. Safe to call repeatedly."""
        path = self.raw_dir / f"{match_id}.json"
        # Write via a temp file so an interrupted crawl never leaves a
        # half-written payload that would fail to parse on the next load.
        # The temp name carries the pid and a counter: two crawlers running
        # at once would otherwise collide on the same path, and on Windows
        # the rename fails outright rather than silently winning.
        tmp = path.with_suffix(f".json.{os.getpid()}.{next(_tmp_counter)}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            # On Windows the rename fails if anything else has the
            # destination open, which happens when two crawlers reach the
            # same match at once. Retry briefly; the content is identical
            # either way, so losing the race is harmless.
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        if path.exists():
                            # Someone else wrote it; that is good enough.
                            tmp.unlink(missing_ok=True)
                            break
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO matches (match_id, map_name, mode, queue, region,
                                     started_at, rounds, kills, plants, source,
                                     payload_path, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(match_id) DO UPDATE SET
                    map_name=excluded.map_name, mode=excluded.mode,
                    kills=excluded.kills, plants=excluded.plants,
                    payload_path=excluded.payload_path
                """,
                (
                    match_id,
                    summary.get("map_name", ""),
                    summary.get("mode", ""),
                    summary.get("queue", ""),
                    summary.get("region", ""),
                    summary.get("started_at", 0),
                    summary.get("rounds", 0),
                    summary.get("kills", 0),
                    summary.get("plants", 0),
                    summary.get("source", ""),
                    str(path.name),
                    _now(),
                ),
            )
        return path

    def iter_payload_paths(self, limit: int | None = None) -> list[Path]:
        sql = "SELECT payload_path FROM matches ORDER BY started_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [self.raw_dir / r["payload_path"] for r in rows]

    def match_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(kills),0) k, COALESCE(SUM(plants),0) p FROM matches"
            ).fetchone()
            by_map = conn.execute(
                """SELECT map_name, COUNT(*) matches, COALESCE(SUM(kills),0) kills
                   FROM matches GROUP BY map_name ORDER BY matches DESC"""
            ).fetchall()
            players = conn.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN crawled_at IS NULL THEN 1 ELSE 0 END) pending
                   FROM players"""
            ).fetchone()
            queue = conn.execute(
                "SELECT state, COUNT(*) n FROM match_queue GROUP BY state"
            ).fetchall()
        return {
            "matches": totals["n"],
            "kills": totals["k"],
            "plants": totals["p"],
            "by_map": [dict(r) for r in by_map],
            "players_known": players["total"] or 0,
            "players_pending": players["pending"] or 0,
            "queue": {r["state"]: r["n"] for r in queue},
        }

    # --- players (crawl frontier) --------------------------------------
    def add_player(
        self, puuid: str, name: str = "", tag: str = "", region: str = "", tier: int = 0
    ) -> None:
        if not puuid:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO players (puuid, name, tag, region, tier, discovered_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(puuid) DO UPDATE SET
                    name=COALESCE(NULLIF(excluded.name,''), players.name),
                    tag=COALESCE(NULLIF(excluded.tag,''), players.tag),
                    tier=MAX(players.tier, excluded.tier)
                """,
                (puuid, name, tag, region, tier, _now()),
            )

    def pending_players(self, limit: int = 20, region: str | None = None) -> list[dict[str, Any]]:
        """Highest-tier uncrawled players first: better data per request."""
        sql = "SELECT * FROM players WHERE crawled_at IS NULL"
        args: list[Any] = []
        if region:
            sql += " AND region = ?"
            args.append(region)
        sql += " ORDER BY tier DESC, discovered_at ASC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def mark_player_crawled(self, puuid: str, match_count: int = 0) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE players SET crawled_at = ?, match_count = ? WHERE puuid = ?",
                (_now(), match_count, puuid),
            )

    # --- match queue ---------------------------------------------------
    def enqueue_match(self, match_id: str, region: str = "") -> bool:
        """Returns True if this is a genuinely new match id."""
        if not match_id or self.has_match(match_id):
            return False
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO match_queue (match_id, region, queued_at) VALUES (?,?,?)
                   ON CONFLICT(match_id) DO NOTHING""",
                (match_id, region, _now()),
            )
            return cur.rowcount > 0

    def pending_matches(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM match_queue WHERE state = 'pending'
                       ORDER BY queued_at ASC LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]

    def mark_match(self, match_id: str, state: str, error: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE match_queue
                   SET state = ?, attempts = attempts + 1, last_error = ?
                   WHERE match_id = ?""",
                (state, error[:500], match_id),
            )


db = Database()
