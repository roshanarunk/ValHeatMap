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

import gzip
import itertools
import json
import os
import re
import sqlite3
import time
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .paths import DATA_DIR
from typing import Any, Iterator

DATA_ROOT = DATA_DIR
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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
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

    @staticmethod
    def _read_file_payload(path: Path) -> dict[str, Any] | None:
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    return json.load(fh)
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def get_payload(self, match_id: str) -> dict[str, Any] | None:
        """Load the raw JSON payload for a match from loose file or zip archive."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_path FROM matches WHERE match_id = ?", (match_id,)
            ).fetchone()

        payload_path = row["payload_path"] if row else None

        # 1. Stored in a zip archive (e.g. "archive_0001.zip:match_id.json")
        if payload_path and (":" in payload_path or payload_path.endswith(".zip")):
            zip_name, _, member_name = payload_path.partition(":")
            member = member_name or f"{match_id}.json"
            zip_file = self.raw_dir / zip_name
            if zip_file.is_file():
                try:
                    with zipfile.ZipFile(zip_file, "r") as zf:
                        return json.loads(zf.read(member).decode("utf-8"))
                except (KeyError, zipfile.BadZipFile, json.JSONDecodeError, OSError):
                    pass

        # 2. Stored as loose file
        if payload_path:
            p = self.raw_dir / payload_path
            if p.is_file():
                return self._read_file_payload(p)

        # 3. Fallbacks: check common paths in raw_dir
        for candidate in (
            self.raw_dir / f"{match_id}.json",
            self.raw_dir / f"{match_id}.json.gz",
        ):
            if candidate.is_file():
                return self._read_file_payload(candidate)

        return None

    def iter_payload_paths(self, limit: int | None = None) -> list[Path]:
        sql = "SELECT payload_path FROM matches ORDER BY started_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        paths = []
        for r in rows:
            pp = r["payload_path"]
            if ":" in pp or pp.endswith(".zip"):
                zip_name, _, _ = pp.partition(":")
                paths.append(self.raw_dir / zip_name)
            else:
                paths.append(self.raw_dir / pp)
        return paths

    def iter_payloads(
        self, limit: int | None = None
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield (match_id, payload_dict) efficiently, keeping zip handles open."""
        sql = "SELECT match_id, payload_path FROM matches ORDER BY started_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()

        current_zip_name: str | None = None
        current_zf: zipfile.ZipFile | None = None

        try:
            for r in rows:
                mid = r["match_id"]
                ppath = r["payload_path"]
                if ":" in ppath or ppath.endswith(".zip"):
                    zip_name, _, member_name = ppath.partition(":")
                    member = member_name or f"{mid}.json"
                    if zip_name != current_zip_name:
                        if current_zf is not None:
                            current_zf.close()
                            current_zf = None
                        current_zip_name = zip_name
                        z_path = self.raw_dir / zip_name
                        if z_path.is_file():
                            try:
                                current_zf = zipfile.ZipFile(z_path, "r")
                            except (zipfile.BadZipFile, OSError):
                                current_zf = None

                    if current_zf is not None:
                        try:
                            payload = json.loads(current_zf.read(member).decode("utf-8"))
                            yield mid, payload
                            continue
                        except (KeyError, json.JSONDecodeError):
                            pass
                else:
                    if current_zf is not None:
                        current_zf.close()
                        current_zf = None
                        current_zip_name = None

                    p = self.raw_dir / ppath
                    payload = self._read_file_payload(p)
                    if payload is not None:
                        yield mid, payload
        finally:
            if current_zf is not None:
                current_zf.close()

    def archive_raw(
        self, chunk_size: int = 1000, max_chunks: int | None = None
    ) -> dict[str, Any]:
        """Pack loose match files into sequence-numbered zip archives.

        Matches are bundled into `archive_NNNN.zip` files (e.g. 1000 per zip),
        the SQLite `payload_path` index is updated, and loose files are removed.
        Each zip part can be independently extracted or inspected.
        """
        # Find matches with loose payload paths (not already in a zip)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT match_id, payload_path FROM matches "
                "WHERE payload_path NOT LIKE '%.zip%' "
                "ORDER BY started_at ASC"
            ).fetchall()

        if not rows:
            return {"archived": 0, "archives_created": 0, "bytes_freed": 0}

        # Determine next archive sequence number
        existing_nums = []
        for p in self.raw_dir.glob("archive_*.zip"):
            m = re.search(r"archive_(\d+)\.zip$", p.name)
            if m:
                existing_nums.append(int(m.group(1)))
        next_idx = (max(existing_nums) + 1) if existing_nums else 1

        archived_total = 0
        archives_created = 0
        bytes_freed = 0

        # Process in chunks
        for i in range(0, len(rows), chunk_size):
            if max_chunks is not None and archives_created >= max_chunks:
                break

            batch = rows[i : i + chunk_size]
            zip_name = f"archive_{next_idx:04d}.zip"
            zip_path = self.raw_dir / zip_name
            tmp_zip = self.raw_dir / f"{zip_name}.tmp.{os.getpid()}"

            batch_success: list[tuple[str, Path]] = []
            try:
                with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                    for r in batch:
                        mid = r["match_id"]
                        p_rel = r["payload_path"]
                        src_path = self.raw_dir / p_rel
                        if not src_path.is_file():
                            if (self.raw_dir / f"{mid}.json").is_file():
                                src_path = self.raw_dir / f"{mid}.json"
                            elif (self.raw_dir / f"{mid}.json.gz").is_file():
                                src_path = self.raw_dir / f"{mid}.json.gz"
                            else:
                                continue

                        if src_path.suffix == ".gz":
                            with gzip.open(src_path, "rb") as gz_in:
                                content = gz_in.read()
                        else:
                            content = src_path.read_bytes()

                        zf.writestr(f"{mid}.json", content)
                        batch_success.append((mid, src_path))

                if not batch_success:
                    tmp_zip.unlink(missing_ok=True)
                    continue

                # Verify zip integrity before committing
                with zipfile.ZipFile(tmp_zip, "r") as zf:
                    if zf.testzip() is not None:
                        raise RuntimeError(f"Corrupted zip generated: {tmp_zip}")

                os.replace(tmp_zip, zip_path)

                # Update DB in one transaction
                updates = [
                    (f"{zip_name}:{mid}.json", mid)
                    for mid, _ in batch_success
                ]
                with self.connect() as conn:
                    conn.executemany(
                        "UPDATE matches SET payload_path = ? WHERE match_id = ?",
                        updates,
                    )

                # Delete loose files
                for _, src_file in batch_success:
                    try:
                        bytes_freed += src_file.stat().st_size
                        src_file.unlink(missing_ok=True)
                    except OSError:
                        pass

                archived_total += len(batch_success)
                archives_created += 1
                next_idx += 1

            except Exception:
                tmp_zip.unlink(missing_ok=True)
                raise

        return {
            "archived": archived_total,
            "archives_created": archives_created,
            "bytes_freed": bytes_freed,
        }

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
