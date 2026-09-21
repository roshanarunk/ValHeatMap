"""Queryable analytics database.

Why this exists
---------------
The raw payloads are the source of truth, but they are 2.5 GB of JSON and
re-parsing them takes ~41s -- fine for a long-lived local process, fatal for
a serverless cold start. This module derives a flat, indexed table with one
row per kill (and per plant) so the API answers a heatmap query with an
indexed SELECT instead of a full re-parse.

It is a *derived* store: it can be rebuilt from `data/raw` at any time, and
the crawler appends to it as matches arrive, so it is never stale.

Size: ~70 bytes per kill row, so 1M kills is ~75 MB -- small enough to ship
to object storage and read from a read-only filesystem.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .paths import DATA_DIR
from typing import Any, Iterator, Sequence

from .analytics.kills import EnrichedKill, enrich
from .models import DamageType, Match, Side

DEFAULT_PATH = DATA_DIR / "analytics.db"

# Kill flags packed into one integer column instead of five.
FLAG_TRADED = 1
FLAG_TRADE_KILL = 2
FLAG_FIRST_BLOOD = 4
FLAG_POST_PLANT = 8
FLAG_ROUND_WON = 16

SIDE_ID = {Side.NONE: 0, Side.ATTACK: 1, Side.DEFENSE: 2}
SIDE_NAME = {0: "none", 1: "attack", 2: "defense"}
DMG_ID = {
    DamageType.WEAPON: 0,
    DamageType.ABILITY: 1,
    DamageType.BOMB: 2,
    DamageType.FALL: 3,
    DamageType.UNKNOWN: 3,
}
DMG_NAME = {0: "weapon", 1: "ability", 2: "bomb", 3: "other"}

# Positions are stored as integers scaled by this factor. 10000 keeps
# precision far below a pixel while costing 2 bytes instead of 8.
POS_SCALE = 10000


def to_pos(value: float) -> int:
    return int(round(value * POS_SCALE))


def from_pos(value: int | None) -> float | None:
    return None if value is None else value / POS_SCALE

SCHEMA = """
PRAGMA journal_mode=WAL;

-- Small lookup tables. Map/agent/weapon names repeat on every kill row, so
-- storing them as ids instead of text cuts the table roughly in half and
-- shrinks every index that covers them.
CREATE TABLE IF NOT EXISTS dim (
    kind  TEXT NOT NULL,            -- map | agent | weapon | ability | act | patch
    id    INTEGER NOT NULL,
    name  TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dim_name ON dim(kind, name);

CREATE TABLE IF NOT EXISTS matches (
    id           INTEGER PRIMARY KEY,   -- compact surrogate key
    match_id     TEXT NOT NULL UNIQUE,
    map_id       INTEGER NOT NULL,
    mode         TEXT NOT NULL,
    queue        TEXT,
    act_id       INTEGER,
    patch_id     INTEGER,
    region       TEXT,
    started_at   INTEGER,
    avg_tier     INTEGER,
    rounds       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_m_map ON matches(map_id, avg_tier);

-- One row per kill. Positions are already projected into minimap space
-- [0,1] so the API never repeats the transform.
CREATE TABLE IF NOT EXISTS kills (
    m            INTEGER NOT NULL,      -- matches.id
    map_id       INTEGER NOT NULL,
    act_id       INTEGER,
    avg_tier     INTEGER,
    round_num    INTEGER NOT NULL,
    t_ms         INTEGER NOT NULL,
    side         INTEGER NOT NULL,      -- 0 none, 1 attack, 2 defense
    ka_id        INTEGER,               -- killer agent
    va_id        INTEGER,               -- victim agent
    weapon_id    INTEGER,
    ability_id   INTEGER,
    dmg_type     INTEGER NOT NULL,      -- 0 weapon 1 ability 2 bomb 3 other
    -- Positions as integers in [0, POS_SCALE], not REAL. SQLite stores a
    -- REAL in 8 bytes; these fit in 2, which over millions of rows is the
    -- difference between the published database fitting in a serverless
    -- function's /tmp and not. The minimap is ~1000px, so 1/10000 is well
    -- below one pixel of precision.
    vx           INTEGER NOT NULL,
    vy           INTEGER NOT NULL,
    kx           INTEGER,
    ky           INTEGER,
    flags        INTEGER NOT NULL,      -- bitfield, see FLAG_* below
    -- Who actually played, interned through dim(kind='player') like every
    -- other repeated string. A puuid is 36 bytes and appears twice per
    -- kill; as ids that is 8 bytes instead of 72, which over ~5M rows is
    -- the difference between ~700 MB and ~80 MB. Nullable because the
    -- aggregate data collected before this existed has no attribution.
    killer_pid   INTEGER,
    victim_pid   INTEGER
);
-- One covering index for the hot path: every heatmap query filters on map
-- first, then narrows. A single composite beats several overlapping ones,
-- which is what made the first version's indexes larger than its data.
CREATE INDEX IF NOT EXISTS idx_k_main ON kills(map_id, avg_tier, act_id, t_ms);
CREATE INDEX IF NOT EXISTS idx_k_m    ON kills(m);
-- Spatial lookups for zone selection. Without these, "which kills happened
-- inside this box" is a full scan: 655ms over 2.6M rows versus 7ms here,
-- which is the difference between a laggy drag and an instant one.
CREATE INDEX IF NOT EXISTS idx_k_vpos ON kills(map_id, vx, vy);
CREATE INDEX IF NOT EXISTS idx_k_kpos ON kills(map_id, kx, ky);
-- Ability kills are ~1% of rows, so a partial index stays tiny while
-- turning the utility view from three full scans (2.4s) into 54ms.
CREATE INDEX IF NOT EXISTS idx_k_util ON kills(map_id, ability_id, avg_tier, act_id)
    WHERE dmg_type = 1;
-- Personal stats: "my kills" and "my deaths" on a given map. Partial, so
-- they cost nothing for the rows crawled before attribution existed --
-- which is most of them, and all of them until the backfill runs.
CREATE INDEX IF NOT EXISTS idx_k_killer ON kills(killer_pid, map_id)
    WHERE killer_pid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_k_victim ON kills(victim_pid, map_id)
    WHERE victim_pid IS NOT NULL;

CREATE TABLE IF NOT EXISTS plants (
    m          INTEGER NOT NULL,
    map_id     INTEGER NOT NULL,
    act_id     INTEGER,
    avg_tier   INTEGER,
    round_num  INTEGER NOT NULL,
    t_ms       INTEGER NOT NULL,
    site       TEXT NOT NULL,
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    won        INTEGER NOT NULL,
    defused    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_p_main ON plants(map_id, avg_tier, act_id);
CREATE INDEX IF NOT EXISTS idx_p_m    ON plants(m);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- People who have asked to see their own stats. The crawler prioritises
-- these players, and `requested_at` is what it sorts by, so someone who
-- has just registered is served before the general discovery crawl.
CREATE TABLE IF NOT EXISTS tracked_players (
    puuid        TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    tag          TEXT NOT NULL,
    region       TEXT,
    pid          INTEGER,            -- dim(kind='player') id, for joins
    requested_at INTEGER NOT NULL,   -- unix seconds, first registration
    last_seen_at INTEGER,            -- last time anyone opened their page
    crawled_at   INTEGER,            -- last successful history fetch
    match_count  INTEGER DEFAULT 0
);
-- The crawler's work queue: who needs fetching, oldest crawl first.
CREATE INDEX IF NOT EXISTS idx_tracked_pending
    ON tracked_players(crawled_at, requested_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_riot_id
    ON tracked_players(name, tag);

-- Facet counts, precomputed. Deriving them means grouping over every kill
-- row: ~2.3s locally and over 7s on a serverless function's slower disk,
-- on a request the UI makes before it can render anything. They only
-- change when the database does, so they are computed once at build time
-- and read back as a single row.
CREATE TABLE IF NOT EXISTS facet_cache (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an existing database up to the current schema.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
    exists, so a column added to SCHEMA never reaches a database built
    before it. That is not theoretical: switching positions to integers
    appeared to work and silently did not, until every database was
    rebuilt from scratch. Cheap, additive changes belong here instead.

    Returns the statements applied, so callers can report them.
    """
    applied: list[str] = []
    columns = {row[1] for row in conn.execute("PRAGMA table_info(kills)")}
    if not columns:
        return applied  # fresh database; SCHEMA builds it correctly
    for column in ("killer_pid", "victim_pid"):
        if column not in columns:
            # NULL for every existing row: attribution for those comes
            # from the backfill, which re-reads the raw payloads.
            conn.execute(f"ALTER TABLE kills ADD COLUMN {column} INTEGER")
            applied.append(f"kills.{column}")
    return applied


class AnalyticsDB:
    def __init__(self, path: Path | None = None, read_only: bool = False) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._dim_cache: dict[str, dict[str, int]] = {}
        if not read_only:
            with self.connect() as conn:
                # Migrate first: SCHEMA creates indexes over the new
                # columns, which fails outright on a database that predates
                # them. On a fresh database this is a no-op.
                _migrate(conn)
                conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self.read_only:
                # immutable=1 tells SQLite the file will not change, which
                # skips locking entirely -- required on a read-only FS.
                conn = sqlite3.connect(
                    f"file:{self.path}?immutable=1", uri=True, check_same_thread=False
                )
            else:
                conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
            if not self.read_only:
                conn.commit()
        except Exception:
            if not self.read_only:
                conn.rollback()
                # The rollback may have discarded dim rows this transaction
                # inserted, while the cache still holds their ids. Keeping
                # it would hand out ids for rows that no longer exist, so
                # drop it and let the next call re-read committed state.
                self._dim_cache.clear()
            raise

    # --- dimension interning -------------------------------------------
    def _dim_id(self, conn: sqlite3.Connection, kind: str, name: str | None) -> int | None:
        """Map a name to a small integer id, creating it on first sight."""
        if not name:
            return None
        cache = self._dim_cache.setdefault(kind, {})
        hit = cache.get(name)
        if hit is not None:
            return hit
        # Not cached: look it up, and insert only if it is genuinely absent.
        # The lookup below is also what repairs a cache entry left behind by
        # a rolled-back transaction, since `invalidate_dim_cache` drops the
        # stale entry and sends the next call back through here.
        row = conn.execute(
            "SELECT id FROM dim WHERE kind=? AND name=?", (kind, name)
        ).fetchone()
        if row is None:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 n FROM dim WHERE kind=?", (kind,)
            ).fetchone()["n"]
            # DO NOTHING rather than a plain INSERT: the process cache can
            # outlive the transaction that wrote a row, so after a rollback
            # this asks for a name the cache thinks exists. Re-reading the
            # committed id is correct; failing on the conflict is not.
            conn.execute(
                "INSERT INTO dim (kind, id, name) VALUES (?,?,?) "
                "ON CONFLICT(kind, name) DO NOTHING",
                (kind, nxt, name),
            )
            row = conn.execute(
                "SELECT id FROM dim WHERE kind=? AND name=?", (kind, name)
            ).fetchone()
            value = row["id"] if row else nxt
        else:
            value = row["id"]
        cache[name] = value
        return value

    def dim_names(self, kind: str) -> dict[int, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id, name FROM dim WHERE kind=?", (kind,)).fetchall()
        return {r["id"]: r["name"] for r in rows}

    def dim_ids(self, kind: str) -> dict[str, int]:
        return {v: k for k, v in self.dim_names(kind).items()}

    # --- ingest --------------------------------------------------------
    def has_match(self, match_id: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
                ).fetchone()
                is not None
            )

    def match_row_id(self, match_id: str) -> int | None:
        """The compact `matches.id` for a Riot match id, or None."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM matches WHERE match_id = ?", (match_id,)
            ).fetchone()
        return row["id"] if row else None

    def add_match(
        self, match: Match, map_info, enriched: Sequence[EnrichedKill] | None = None
    ) -> int:
        """Derive and store every row for one match. Returns kills written."""
        if map_info is None or not map_info.has_calibration:
            return 0
        meta = match.meta
        avg_tier = _avg_tier(match)
        rows = enriched if enriched is not None else enrich(match)

        with self.connect() as conn:
            map_id = self._dim_id(conn, "map", meta.map_name)
            if map_id is None:
                return 0
            act_id = self._dim_id(conn, "act", meta.act or None)
            patch_id = self._dim_id(conn, "patch", _patch_of(meta.game_version) or None)

            conn.execute(
                """INSERT INTO matches (match_id, map_id, mode, queue, act_id, patch_id,
                                        region, started_at, avg_tier, rounds)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(match_id) DO UPDATE SET
                     map_id=excluded.map_id, act_id=excluded.act_id,
                     avg_tier=excluded.avg_tier""",
                (
                    meta.match_id, map_id, meta.mode, meta.queue, act_id, patch_id,
                    meta.region, meta.started_at, avg_tier, len(match.rounds),
                ),
            )
            m = conn.execute(
                "SELECT id FROM matches WHERE match_id=?", (meta.match_id,)
            ).fetchone()["id"]

            # Re-ingesting a match replaces its rows rather than duplicating.
            conn.execute("DELETE FROM kills WHERE m = ?", (m,))
            conn.execute("DELETE FROM plants WHERE m = ?", (m,))

            kill_rows = []
            for ek in rows:
                k = ek.kill
                if k.victim_location is None:
                    continue
                vx, vy = map_info.to_minimap(k.victim_location.x, k.victim_location.y)
                if not (0.0 <= vx <= 1.0 and 0.0 <= vy <= 1.0):
                    continue
                kx = ky = None
                if k.killer_location is not None:
                    cx, cy = map_info.to_minimap(k.killer_location.x, k.killer_location.y)
                    if 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0:
                        kx, ky = cx, cy
                flags = (
                    (FLAG_TRADED if ek.traded else 0)
                    | (FLAG_TRADE_KILL if ek.trade_kill else 0)
                    | (FLAG_FIRST_BLOOD if ek.first_blood else 0)
                    | (FLAG_POST_PLANT if ek.post_plant else 0)
                    | (FLAG_ROUND_WON if ek.round_won else 0)
                )
                kill_rows.append(
                    (
                        m, map_id, act_id, avg_tier,
                        k.round_num, k.time_in_round_ms,
                        SIDE_ID.get(k.killer_side, 0),
                        self._dim_id(conn, "agent", ek.killer_agent),
                        self._dim_id(conn, "agent", ek.victim_agent),
                        self._dim_id(conn, "weapon", k.weapon_name or None),
                        self._dim_id(conn, "ability", k.ability_name or None),
                        DMG_ID.get(k.damage_type, 3),
                        # 4 decimals is ~0.1px on a 1000px minimap: plenty of
                        # precision, and it keeps the stored floats short.
                        to_pos(vx), to_pos(vy),
                        None if kx is None else to_pos(kx),
                        None if ky is None else to_pos(ky),
                        flags,
                        self._dim_id(conn, "player", k.killer_puuid or None),
                        self._dim_id(conn, "player", k.victim_puuid or None),
                    )
                )

            plant_rows = []
            for p in match.plants:
                px, py = map_info.to_minimap(p.location.x, p.location.y)
                if not (0.0 <= px <= 1.0 and 0.0 <= py <= 1.0):
                    continue
                plant_rows.append(
                    (
                        m, map_id, act_id, avg_tier, p.round_num, p.round_time_ms,
                        p.site, to_pos(px), to_pos(py), int(p.won), int(p.defused),
                    )
                )

            if kill_rows:
                # Columns named rather than positional: a bare VALUES list
                # breaks silently the next time the table gains a column.
                conn.executemany(
                    """INSERT INTO kills (
                           m, map_id, act_id, avg_tier, round_num, t_ms, side,
                           ka_id, va_id, weapon_id, ability_id, dmg_type,
                           vx, vy, kx, ky, flags, killer_pid, victim_pid
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    kill_rows,
                )
            if plant_rows:
                conn.executemany(
                    "INSERT INTO plants VALUES (?,?,?,?,?,?,?,?,?,?,?)", plant_rows
                )
        return len(kill_rows)

    # --- tracked players -----------------------------------------------
    def track_player(
        self, puuid: str, name: str, tag: str, region: str | None = None
    ) -> dict[str, Any]:
        """Register someone for personal stats, or touch them if known.

        `requested_at` is set once and never moved, so the crawler's
        ordering reflects who has been waiting longest rather than who
        refreshed the page most recently.
        """
        now = int(time.time())
        with self.connect() as conn:
            pid = self._dim_id(conn, "player", puuid)
            conn.execute(
                """INSERT INTO tracked_players
                       (puuid, name, tag, region, pid, requested_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(puuid) DO UPDATE SET
                       name=excluded.name, tag=excluded.tag,
                       region=COALESCE(excluded.region, tracked_players.region),
                       pid=excluded.pid,
                       last_seen_at=excluded.last_seen_at""",
                (puuid, name, tag, region, pid, now, now),
            )
            row = conn.execute(
                "SELECT * FROM tracked_players WHERE puuid = ?", (puuid,)
            ).fetchone()
        return dict(row)

    def tracked_player(self, name: str, tag: str) -> dict[str, Any] | None:
        """Look someone up by Riot ID. Case-insensitive, as Riot IDs are."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tracked_players "
                "WHERE LOWER(name) = LOWER(?) AND LOWER(tag) = LOWER(?)",
                (name, tag),
            ).fetchone()
        return dict(row) if row else None

    def tracked_by_puuid(self, puuid: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tracked_players WHERE puuid = ?", (puuid,)
            ).fetchone()
        return dict(row) if row else None

    def players_needing_crawl(self, limit: int = 5, max_age_s: int = 900) -> list[dict[str, Any]]:
        """Tracked players due a history fetch, longest-waiting first.

        Never-crawled players sort first because someone who has just
        registered is watching an empty page.
        """
        cutoff = int(time.time()) - max_age_s
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM tracked_players
                   WHERE crawled_at IS NULL OR crawled_at < ?
                   ORDER BY crawled_at IS NOT NULL, crawled_at, requested_at
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_player_crawled(self, puuid: str, match_count: int | None = None) -> None:
        with self.connect() as conn:
            if match_count is None:
                conn.execute(
                    "UPDATE tracked_players SET crawled_at = ? WHERE puuid = ?",
                    (int(time.time()), puuid),
                )
            else:
                conn.execute(
                    "UPDATE tracked_players SET crawled_at = ?, match_count = ? "
                    "WHERE puuid = ?",
                    (int(time.time()), match_count, puuid),
                )

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def optimise(self) -> None:
        """Compact and analyse. Worth running once after a bulk build."""
        conn = self._conn()
        conn.execute("ANALYZE")
        conn.commit()
        previous = conn.isolation_level
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.isolation_level = previous

    # --- query ---------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            m = conn.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]
            k = conn.execute("SELECT COUNT(*) n FROM kills").fetchone()["n"]
            p = conn.execute("SELECT COUNT(*) n FROM plants").fetchone()["n"]
        return {
            "matches": m,
            "kills": k,
            "plants": p,
            "generated_at": self.get_meta("generated_at"),
        }

    def rebuild_facet_cache(self) -> dict[str, Any]:
        """Compute the facet payload and store it for instant reads."""
        data = self._compute_facets()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO facet_cache (key, value) VALUES ('facets', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(data),),
            )
        return data

    def facets(self) -> dict[str, Any]:
        """Distinct filter values present in the data, for the UI.

        Served from the precomputed cache when present; falls back to
        computing on demand so an older database still works.
        """
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM facet_cache WHERE key = 'facets'"
                ).fetchone()
            if row:
                cached = json.loads(row["value"])
                # On a server the crawler writes to this same file, so the
                # cache goes stale as matches arrive. Recompute when the
                # match count has moved enough to matter; the counts are
                # only used to populate filter lists, so being a little
                # behind is fine but being thousands behind is not.
                if not self._facets_are_stale(cached):
                    return cached
        except (sqlite3.Error, json.JSONDecodeError):
            pass  # missing or corrupt cache: fall through and compute
        return self._compute_facets()

    def _facets_are_stale(self, cached: dict[str, Any], tolerance: int = 500) -> bool:
        """Has the dataset moved far enough to be worth recomputing?"""
        try:
            cached_total = sum(m.get("matches", 0) for m in cached.get("maps", []))
            if cached_total == 0:
                return True
            with self.connect() as conn:
                actual = conn.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]
            return abs(actual - cached_total) > tolerance
        except sqlite3.Error:
            return False

    def _compute_facets(self) -> dict[str, Any]:
        """Distinct filter values present in the data, for populating the UI."""
        maps = self.dim_names("map")
        acts = self.dim_names("act")
        agents = self.dim_names("agent")
        abilities = self.dim_names("ability")
        weapons = self.dim_names("weapon")
        with self.connect() as conn:
            map_rows = conn.execute(
                "SELECT map_id, COUNT(*) matches FROM matches GROUP BY map_id"
            ).fetchall()
            kill_rows = conn.execute(
                "SELECT map_id, COUNT(*) kills FROM kills GROUP BY map_id"
            ).fetchall()
            act_rows = conn.execute(
                "SELECT act_id, COUNT(*) matches FROM matches "
                "WHERE act_id IS NOT NULL GROUP BY act_id"
            ).fetchall()
            agent_rows = conn.execute(
                "SELECT ka_id, COUNT(*) kills FROM kills "
                "WHERE ka_id IS NOT NULL GROUP BY ka_id ORDER BY kills DESC"
            ).fetchall()
            weapon_rows = conn.execute(
                "SELECT weapon_id, COUNT(*) kills FROM kills "
                "WHERE weapon_id IS NOT NULL GROUP BY weapon_id ORDER BY kills DESC"
            ).fetchall()
            ability_rows = conn.execute(
                "SELECT ability_id, ka_id, COUNT(*) kills FROM kills "
                "WHERE ability_id IS NOT NULL GROUP BY ability_id, ka_id "
                "ORDER BY kills DESC"
            ).fetchall()
            tiers = conn.execute(
                "SELECT MIN(avg_tier) lo, MAX(avg_tier) hi FROM matches WHERE avg_tier > 0"
            ).fetchone()
        kills_by_map = {r["map_id"]: r["kills"] for r in kill_rows}
        return {
            "maps": sorted(
                (
                    {
                        "map_name": maps.get(r["map_id"], "?"),
                        "matches": r["matches"],
                        "kills": kills_by_map.get(r["map_id"], 0),
                    }
                    for r in map_rows
                ),
                key=lambda d: -d["kills"],
            ),
            # Acts sort numerically, not lexically: "e11a5" must come after
            # "e9a3", which a plain string sort gets backwards.
            "acts": sorted(
                ({"act": acts.get(r["act_id"], "?"), "matches": r["matches"]} for r in act_rows),
                key=lambda d: _act_sort_key(d["act"]),
                reverse=True,
            ),
            "agents": [
                {"agent": agents.get(r["ka_id"], "?"), "kills": r["kills"]} for r in agent_rows
            ],
            "weapons": [
                {"weapon": weapons.get(r["weapon_id"], "?"), "kills": r["kills"]}
                for r in weapon_rows
            ],
            "abilities": [
                {
                    "ability": abilities.get(r["ability_id"], "?"),
                    "agent": agents.get(r["ka_id"], "?"),
                    "kills": r["kills"],
                }
                for r in ability_rows
            ],
            "tier_range": [tiers["lo"] or 0, tiers["hi"] or 0],
        }


# --- derivation helpers -------------------------------------------------
def _act_sort_key(act: str) -> tuple[int, int]:
    """'e11a5' -> (11, 5), so acts order by episode then act."""
    import re

    m = re.match(r"e(\d+)a(\d+)", (act or "").lower())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _patch_of(version: str) -> str:
    """'release-13.05-shipping-11-5350494' -> '13.05'."""
    if not version:
        return ""
    parts = version.split("-")
    for part in parts:
        if part and part[0].isdigit() and "." in part:
            return part
    return ""


def _avg_tier(match: Match) -> int:
    tiers = [p.tier for p in match.players if p.tier]
    return round(sum(tiers) / len(tiers)) if tiers else 0
