"""Query layer over the analytics database.

Builds parameterised SQL from the UI's filters. Every query is anchored on
`map_id` because that is how the indexes are ordered and how users actually
navigate: pick a map first, then narrow.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .analytics.rotations import get_map_zones
from .analytics_db import (
    DMG_NAME,
    POS_SCALE,
    from_pos,
    FLAG_FIRST_BLOOD,
    FLAG_POST_PLANT,
    FLAG_ROUND_WON,
    FLAG_TRADE_KILL,
    FLAG_TRADED,
    FLAG_SUPPORTED,
    FLAG_ISOLATED,
    FLAG_CROSSFIRE,
    FLAG_ADVANTAGE_DEATH,
    FLAG_CLUTCH_KILL,
    FLAG_LOW_IMPACT,
    SIDE_NAME,
    AnalyticsDB,
)

# Returning every point is wasteful: the heatmap bins them anyway, and the
# payload has to cross the network. Above this we sample deterministically.
MAX_POINTS = 15_000

# Competitive tier ids, for the rank filter.
TIER_BANDS = {
    "iron": (3, 5),
    "bronze": (6, 8),
    "silver": (9, 11),
    "gold": (12, 14),
    "platinum": (15, 17),
    "diamond": (18, 20),
    "ascendant": (21, 23),
    "immortal": (24, 26),
    "radiant": (27, 27),
}


def _zone(params: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Parse `zone=x0,y0,x1,y1` in normalised minimap coordinates."""
    raw = params.get("zone")
    if not raw:
        return None
    parts = str(raw).split(",")
    if len(parts) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(p) for p in parts)
    except ValueError:
        return None
    # Normalise so a box drawn in any direction works.
    lo_x, hi_x = sorted((x0, x1))
    lo_y, hi_y = sorted((y0, y1))
    if hi_x - lo_x <= 0 or hi_y - lo_y <= 0:
        return None
    return (lo_x, lo_y, hi_x, hi_y)


@dataclass(slots=True)
class Filters:
    map_name: str = ""
    acts: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    victim_agents: list[str] = field(default_factory=list)
    weapons: list[str] = field(default_factory=list)
    victim_weapons: list[str] = field(default_factory=list)
    abilities: list[str] = field(default_factory=list)
    sides: list[str] = field(default_factory=list)
    # Zone selection: a box in minimap space, and which end of the duel it
    # applies to. "killer" answers "who did people in this area kill?",
    # "victim" answers "who killed the people who died here?".
    zone: tuple[float, float, float, float] | None = None
    zone_anchor: str = "victim"
    # Personal stats. `player` is a puuid; `player_role` picks which end of
    # the duel it constrains -- "killer" for my kills, "victim" for my
    # deaths, "either" for everything I was involved in.
    player: str = ""
    player_role: str = "killer"
    match_id: str = ""                  # single-match review
    # Agent roles (Duelist, Sentinel, ...). Expanded to their agents at
    # query time rather than stored per kill: role membership comes from
    # reference data and changes when Riot reworks an agent, so deriving
    # it keeps old rows correct.
    roles: list[str] = field(default_factory=list)
    victim_roles: list[str] = field(default_factory=list)
    ranks: list[str] = field(default_factory=list)      # band names
    tier_min: int | None = None
    tier_max: int | None = None
    time_start: int | None = None
    time_end: int | None = None
    rounds: list[int] = field(default_factory=list)
    traded: bool | None = None          # True=only traded, False=only untraded
    first_blood_only: bool = False
    post_plant: bool | None = None
    utility_only: bool = False
    limit: int = MAX_POINTS

    @classmethod
    def from_query(cls, params: dict[str, Any]) -> "Filters":
        def lst(key: str) -> list[str]:
            raw = params.get(key)
            if not raw:
                return []
            if isinstance(raw, str):
                return [v for v in raw.split(",") if v]
            return [str(v) for v in raw]

        def num(key: str) -> int | None:
            v = params.get(key)
            if v in (None, ""):
                return None
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        def flag(key: str) -> bool:
            return str(params.get(key, "")).lower() in {"1", "true", "yes", "on"}

        traded: bool | None = None
        if flag("traded_only"):
            traded = True
        elif flag("untraded_only"):
            traded = False

        post_plant: bool | None = None
        if flag("post_plant_only"):
            post_plant = True
        elif flag("pre_plant_only"):
            post_plant = False

        return cls(
            map_name=str(params.get("map_name") or ""),
            acts=lst("acts"),
            patches=lst("patches"),
            agents=lst("agents"),
            victim_agents=lst("victim_agents"),
            weapons=lst("weapons"),
            victim_weapons=lst("victim_weapons"),
            abilities=lst("abilities"),
            sides=lst("sides"),
            zone=_zone(params),
            zone_anchor=(
                "killer" if str(params.get("zone_anchor", "")).lower() == "killer" else "victim"
            ),
            player=str(params.get("player") or ""),
            player_role=(
                str(params.get("player_role", "")).lower()
                if str(params.get("player_role", "")).lower()
                in {"killer", "victim", "either"}
                else "killer"
            ),
            match_id=str(params.get("match_id") or ""),
            roles=lst("roles"),
            victim_roles=lst("victim_roles"),
            ranks=lst("ranks"),
            tier_min=num("tier_min"),
            tier_max=num("tier_max"),
            time_start=num("time_start"),
            time_end=num("time_end"),
            rounds=[int(r) for r in lst("rounds") if r.lstrip("-").isdigit()],
            traded=traded,
            first_blood_only=flag("first_blood_only"),
            post_plant=post_plant,
            utility_only=flag("utility_only"),
            limit=num("limit") or MAX_POINTS,
        )

    def tier_bounds(self) -> tuple[int | None, int | None]:
        """Combine named rank bands and explicit bounds into one range."""
        lo, hi = self.tier_min, self.tier_max
        if self.ranks:
            bands = [TIER_BANDS[r] for r in self.ranks if r in TIER_BANDS]
            if bands:
                band_lo = min(b[0] for b in bands)
                band_hi = max(b[1] for b in bands)
                lo = band_lo if lo is None else max(lo, band_lo)
                hi = band_hi if hi is None else min(hi, band_hi)
        return lo, hi


class QueryEngine:
    """One instance is shared across every request, now including
    concurrent ones on Starlette's thread pool (route handlers with no
    `await` run as plain `def` so they don't block the event loop; see
    main.py). `_dims` is a plain dict with a check-then-set pattern that
    is not atomic as a whole, so two threads can race to fill the same
    key -- harmless, since both would compute the same value and dict
    item assignment is itself safe under the GIL. Nothing here iterates
    `_dims`, which is the case that would actually be unsafe.
    """

    def __init__(self, db: AnalyticsDB) -> None:
        self.db = db
        self._dims: dict[str, dict[str, int]] = {}
        # Finished responses, as gzipped JSON bytes. Not the result dicts:
        # one heatmap is 15k point dicts, measured at 14.8 MB of Python
        # objects against 0.36 MB compressed. At 256 entries the dicts could
        # outgrow the whole 1 GB machine, and even the warmer's dozen took
        # the memory the OS would otherwise spend caching the database.
        self._query_cache: dict[tuple[Any, ...], bytes] = {}
        self._cache_lock = threading.Lock()
        self._cache_max_entries = 256

    def _ids(self, kind: str) -> dict[str, int]:
        if kind not in self._dims:
            self._dims[kind] = self.db.dim_ids(kind)
        return self._dims[kind]

    def _names(self, kind: str) -> dict[int, str]:
        key = f"~{kind}"
        if key not in self._dims:
            self._dims[key] = self.db.dim_names(kind)  # type: ignore[assignment]
        return self._dims[key]  # type: ignore[return-value]

    def invalidate(self, players_only: bool = False) -> None:
        """Drop cached dimension maps and query results after an ingest adds new names.

        `players_only` is for player events (register, refresh, scout): a
        handful of one player's matches changes nothing visible on the
        map-wide heatmaps, so only responses scoped to a player are
        dropped. Clearing everything made every such event send the next
        visitors to cold queries until the warmer's next pass.
        """
        self._dims.clear()
        with self._cache_lock:
            if players_only:
                for key in [k for k in self._query_cache if k[-1]]:
                    del self._query_cache[key]
            else:
                self._query_cache.clear()

    def cache_key(self, kind: str, f: Filters) -> tuple[Any, ...]:
        """Identity of a response: the resolved SQL rather than the raw
        filters, so equivalent selections share one entry. The player is
        last, which is what `invalidate(players_only=True)` matches on."""
        where, args = self._where(f)
        return (kind, where, tuple(args), f.limit, f.player or None)

    def cached(self, key: tuple[Any, ...], build: Callable[[], bytes]) -> bytes:
        with self._cache_lock:
            hit = self._query_cache.get(key)
        if hit is not None:
            return hit
        body = build()
        with self._cache_lock:
            if len(self._query_cache) >= self._cache_max_entries:
                self._query_cache.pop(next(iter(self._query_cache)), None)
            self._query_cache[key] = body
        return body

    def _role_agent_ids(self, roles: list[str]) -> list[int] | None:
        """Agent ids belonging to any of `roles`, or None if none resolve.

        Returning None rather than an empty list matters for the same
        reason it does in `_resolve`: an unrecognised role has to match
        nothing, not silently drop the filter and return everything.
        """
        from .reference import agents_by_id

        wanted = {r.strip().lower() for r in roles if r.strip()}
        if not wanted:
            return None
        names = {
            a.name for a in agents_by_id().values() if (a.role or "").lower() in wanted
        }
        if not names:
            return None
        return self._resolve("agent", sorted(names))

    def _resolve(self, kind: str, names: list[str]) -> list[int] | None:
        """Names to ids. Returns None when *nothing* resolved.

        The distinction matters: an empty id list must mean "match nothing",
        not "no filter". Dropping an unresolvable filter silently returns the
        whole dataset, which reads as though the filter were ignored.
        """
        ids = [self._ids(kind).get(n) for n in names]
        ids = [i for i in ids if i is not None]
        return ids or None

    # --- SQL building ---------------------------------------------------
    def _where(
        self,
        f: Filters,
        table: str = "kills",
        is_player: bool = False,
        player_pid: int | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []

        map_ids = self._ids("map")
        if f.map_name:
            mid = map_ids.get(f.map_name)
            if mid is None:
                # Unknown map: force an empty result rather than a full scan.
                return "1=0", []
            clauses.append(f"{table}.map_id = ?")
            args.append(mid)

        if f.acts:
            act_ids = self._resolve("act", f.acts)
            if act_ids is None:
                return "1=0", []
            clauses.append(f"{table}.act_id IN ({','.join('?' * len(act_ids))})")
            args.extend(act_ids)

        lo, hi = f.tier_bounds()
        if lo is not None:
            clauses.append(f"{table}.avg_tier >= ?")
            args.append(lo)
        if hi is not None:
            clauses.append(f"{table}.avg_tier <= ?")
            args.append(hi)

        if f.match_id:
            row = self.db.match_row_id(f.match_id)
            if row is None:
                return "1=0", []
            clauses.append(f"{table}.m = ?")
            args.append(row)

        active_pid = player_pid
        if f.player and table == "kills":
            pid = self._ids("player").get(f.player)
            if pid is None:
                # Never crawled, or crawled before attribution existed.
                # Empty is the honest answer; a full scan is not.
                return "1=0", []
            active_pid = pid
            if f.player_role == "victim":
                clauses.append("victim_pid = ?")
                args.append(pid)
            elif f.player_role == "either":
                # Not `killer_pid = ? OR victim_pid = ?`: an OR across two
                # columns cannot use either partial index, so SQLite falls
                # back to scanning a positional one -- 2,026ms against
                # 0.1ms here. A UNION of rowids searches both indexes and
                # deduplicates, which matters because a self-kill would
                # otherwise appear twice.
                clauses.append(
                    f"{table}.rowid IN ("
                    " SELECT rowid FROM kills WHERE killer_pid = ?"
                    " UNION"
                    " SELECT rowid FROM kills WHERE victim_pid = ?)"
                )
                args.extend([pid, pid])
            else:
                clauses.append("killer_pid = ?")
                args.append(pid)

        if table == "kills":
            # ka_id, va_id, and weapon_id have single-column indexes (idx_k_agent,
            # idx_k_weapon) that exist solely for the unconstrained facet-cache GROUP BY.
            # In all filtered queries (by map, player, etc.), SQLite's planner must use
            # the primary selective index (idx_k_main, idx_k_killer, idx_k_vpos, etc.)
            # rather than scanning millions of rows across all maps via idx_k_weapon/idx_k_agent.
            # We suppress index usage on these secondary filters using SQLite unary plus `+`.
            pfx = "+"
            ability_pfx = "" if f.utility_only else "+"

            # Resolve player's agent and role filters
            agent_filter_ids: set[int] | None = None
            if f.agents:
                ids = self._resolve("agent", f.agents)
                if ids is None:
                    return "1=0", []
                agent_filter_ids = set(ids)

            if f.roles:
                ids = self._role_agent_ids(f.roles)
                if ids is None:
                    return "1=0", []
                if agent_filter_ids is not None:
                    agent_filter_ids = agent_filter_ids.intersection(ids)
                    if not agent_filter_ids:
                        return "1=0", []
                else:
                    agent_filter_ids = set(ids)

            if agent_filter_ids is not None:
                sorted_ids = sorted(agent_filter_ids)
                qmarks = ",".join("?" * len(sorted_ids))
                if active_pid is not None:
                    # In player-specific queries, role/agent describes what this player played:
                    # on kills it's ka_id, on deaths it's va_id.
                    if f.player_role == "victim":
                        clauses.append(f"{pfx}va_id IN ({qmarks})")
                        args.extend(sorted_ids)
                    elif f.player_role == "killer":
                        clauses.append(f"{pfx}ka_id IN ({qmarks})")
                        args.extend(sorted_ids)
                    else:
                        clauses.append(
                            f"((killer_pid = ? AND {pfx}ka_id IN ({qmarks})) OR "
                            f"(victim_pid = ? AND {pfx}va_id IN ({qmarks})))"
                        )
                        args.extend([active_pid] + sorted_ids + [active_pid] + sorted_ids)
                else:
                    clauses.append(f"{pfx}ka_id IN ({qmarks})")
                    args.extend(sorted_ids)

            victim_agent_filter_ids: set[int] | None = None
            if f.victim_agents:
                ids = self._resolve("agent", f.victim_agents)
                if ids is None:
                    return "1=0", []
                victim_agent_filter_ids = set(ids)

            if f.victim_roles:
                ids = self._role_agent_ids(f.victim_roles)
                if ids is None:
                    return "1=0", []
                if victim_agent_filter_ids is not None:
                    victim_agent_filter_ids = victim_agent_filter_ids.intersection(ids)
                    if not victim_agent_filter_ids:
                        return "1=0", []
                else:
                    victim_agent_filter_ids = set(ids)

            if victim_agent_filter_ids is not None:
                sorted_v_ids = sorted(victim_agent_filter_ids)
                qmarks_v = ",".join("?" * len(sorted_v_ids))
                if active_pid is not None:
                    # In player queries, victim_agent/role filters the opponent:
                    # on kills opponent is va_id, on deaths opponent is ka_id.
                    if f.player_role == "victim":
                        clauses.append(f"{pfx}ka_id IN ({qmarks_v})")
                        args.extend(sorted_v_ids)
                    elif f.player_role == "killer":
                        clauses.append(f"{pfx}va_id IN ({qmarks_v})")
                        args.extend(sorted_v_ids)
                    else:
                        clauses.append(
                            f"((killer_pid = ? AND {pfx}va_id IN ({qmarks_v})) OR "
                            f"(victim_pid = ? AND {pfx}ka_id IN ({qmarks_v})))"
                        )
                        args.extend([active_pid] + sorted_v_ids + [active_pid] + sorted_v_ids)
                else:
                    clauses.append(f"{pfx}va_id IN ({qmarks_v})")
                    args.extend(sorted_v_ids)
            if f.weapons:
                ids = self._resolve("weapon", f.weapons)
                if ids is None:
                    return "1=0", []
                clauses.append(f"{pfx}weapon_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            if f.victim_weapons:
                ids = self._resolve("weapon", f.victim_weapons)
                if ids is None:
                    return "1=0", []
                clauses.append(f"{pfx}vw_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            if f.abilities:
                ids = self._resolve("ability", f.abilities)
                if ids is None:
                    return "1=0", []
                clauses.append(f"{ability_pfx}ability_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            if f.zone:
                # The box constrains one end of the duel; the caller plots
                # the other, which is what makes this a cross-filter rather
                # than a plain crop.
                lo_x, lo_y, hi_x, hi_y = (v * POS_SCALE for v in f.zone)
                px, py = ("kx", "ky") if f.zone_anchor == "killer" else ("vx", "vy")
                clauses.append(
                    f"{px} BETWEEN ? AND ? AND {py} BETWEEN ? AND ? AND {px} IS NOT NULL"
                )
                args.extend([lo_x, hi_x, lo_y, hi_y])
            if f.sides:
                codes = [k for k, v in SIDE_NAME.items() if v in f.sides]
                if codes:
                    clauses.append(f"side IN ({','.join('?' * len(codes))})")
                    args.extend(codes)
            if f.time_start is not None:
                clauses.append("t_ms >= ?")
                args.append(f.time_start)
            if f.time_end is not None:
                clauses.append("t_ms <= ?")
                args.append(f.time_end)
            if f.rounds:
                clauses.append(f"round_num IN ({','.join('?' * len(f.rounds))})")
                args.extend(f.rounds)
            if f.traded is True:
                clauses.append(f"(flags & {FLAG_TRADED}) != 0")
            elif f.traded is False:
                clauses.append(f"(flags & {FLAG_TRADED}) = 0")
            if f.first_blood_only:
                clauses.append(f"(flags & {FLAG_FIRST_BLOOD}) != 0")
            if f.post_plant is True:
                clauses.append(f"(flags & {FLAG_POST_PLANT}) != 0")
            elif f.post_plant is False:
                clauses.append(f"(flags & {FLAG_POST_PLANT}) = 0")
            if f.utility_only:
                clauses.append("dmg_type = 1")

        return (" AND ".join(clauses) if clauses else "1=1"), args

    # --- public queries -------------------------------------------------
    def kill_points(self, f: Filters) -> dict[str, Any]:
        where, args = self._where(f)
        with self.db.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) n FROM kills WHERE {where}", args
            ).fetchone()["n"]

            # Deterministic sampling when a selection is huge: taking every
            # Nth row by rowid is uniform across the map and stable between
            # requests, unlike RANDOM().
            sample_sql = ""
            if total > f.limit and f.limit > 0:
                stride = math.ceil(total / f.limit)
                sample_sql = f" AND (rowid % {stride}) = 0 LIMIT {int(f.limit)}"
            elif f.limit > 0 and not sample_sql:
                sample_sql = f" LIMIT {int(f.limit)}"

            rows = conn.execute(
                f"""SELECT m, t_ms, side, ka_id, va_id, weapon_id, ability_id, dmg_type,
                           vx, vy, kx, ky, flags, round_num, killer_pid, victim_pid, vw_id
                    FROM kills WHERE {where}{sample_sql}""",
                args,
            ).fetchall()

        agents = self._names("agent")
        weapons = self._names("weapon")
        abilities = self._names("ability")
        # When the caller asked about one player, say which end of each duel
        # they were on. A boolean rather than the puuid: the client only
        # needs "was this mine", and repeating a 36-byte id on every point
        # would be most of the payload.
        subject_pid = self._ids("player").get(f.player) if f.player else None
        points = [
            {
                "t": r["t_ms"],
                "round": r["round_num"],
                **(
                    {"mine": r["killer_pid"] == subject_pid}
                    if subject_pid is not None
                    else {}
                ),
                "side": SIDE_NAME.get(r["side"], "none"),
                "killer_agent": agents.get(r["ka_id"], ""),
                "victim_agent": agents.get(r["va_id"], ""),
                "weapon": weapons.get(r["weapon_id"], ""),
                "victim_weapon": weapons.get(r["vw_id"], "") if ("vw_id" in r.keys() and r["vw_id"] is not None) else "",
                "ability": abilities.get(r["ability_id"], ""),
                "type": DMG_NAME.get(r["dmg_type"], "other"),
                "victim_pos": {"x": from_pos(r["vx"]), "y": from_pos(r["vy"])},
                "killer_pos": (
                    {"x": from_pos(r["kx"]), "y": from_pos(r["ky"])}
                    if r["kx"] is not None
                    else None
                ),
                "traded": bool(r["flags"] & FLAG_TRADED),
                "first_blood": bool(r["flags"] & FLAG_FIRST_BLOOD),
                "post_plant": bool(r["flags"] & FLAG_POST_PLANT),
            }
            for r in rows
        ]

        # Compute summary stats and histogram in a single pass from the rows
        n_sample = len(rows)
        scale = (total / n_sample) if (n_sample and total > n_sample) else 1.0

        traded_cnt = 0
        fb_cnt = 0
        pp_cnt = 0
        util_cnt = 0
        matches_set: set[int] = set()
        hist_buckets: dict[int, int] = {}

        for r in rows:
            fl = r["flags"]
            if fl & FLAG_TRADED:
                traded_cnt += 1
            if fl & FLAG_FIRST_BLOOD:
                fb_cnt += 1
            if fl & FLAG_POST_PLANT:
                pp_cnt += 1
            if r["dmg_type"] == 1:
                util_cnt += 1
            matches_set.add(r["m"])
            t_bin = (r["t_ms"] // 5000) * 5000
            hist_buckets[t_bin] = hist_buckets.get(t_bin, 0) + 1

        stats = {
            "total": total,
            "matches": round(len(matches_set) * scale) if total > n_sample else len(matches_set),
            "traded": round(traded_cnt * scale),
            "trade_rate": round(traded_cnt / n_sample, 4) if n_sample else 0.0,
            "first_bloods": round(fb_cnt * scale),
            "post_plant": round(pp_cnt * scale),
            "utility": round(util_cnt * scale),
            "utility_rate": round(util_cnt / n_sample, 4) if n_sample else 0.0,
        }

        histogram = [
            {"t": t, "count": round(hist_buckets.get(t, 0) * scale)}
            for t in sorted(hist_buckets)
        ]

        return {
            "points": points,
            "total": total,
            "sampled": len(points) < total,
            "stats": stats,
            "histogram": histogram,
        }

    def summary(self, f: Filters) -> dict[str, Any]:
        where, args = self._where(f)
        with self.db.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) total,
                           SUM((flags & {FLAG_TRADED}) != 0) traded,
                           SUM((flags & {FLAG_FIRST_BLOOD}) != 0) first_bloods,
                           SUM((flags & {FLAG_POST_PLANT}) != 0) post_plant,
                           SUM(dmg_type = 1) utility,
                           COUNT(DISTINCT m) matches
                    FROM kills WHERE {where}""",
                args,
            ).fetchone()
        total = row["total"] or 0
        return {
            "total": total,
            "matches": row["matches"] or 0,
            "traded": row["traded"] or 0,
            "trade_rate": round((row["traded"] or 0) / total, 4) if total else 0.0,
            "first_bloods": row["first_bloods"] or 0,
            "post_plant": row["post_plant"] or 0,
            "utility": row["utility"] or 0,
            "utility_rate": round((row["utility"] or 0) / total, 4) if total else 0.0,
        }

    def histogram(self, f: Filters, bucket_ms: int = 5000) -> list[dict[str, int]]:
        where, args = self._where(f)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT (t_ms / {bucket_ms}) * {bucket_ms} t, COUNT(*) count
                    FROM kills WHERE {where} GROUP BY t ORDER BY t""",
                args,
            ).fetchall()
        return [{"t": r["t"], "count": r["count"]} for r in rows]

    def plants(self, f: Filters) -> list[dict[str, Any]]:
        where, args = self._where(f, table="plants")
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT round_num, t_ms, site, x, y, won, defused
                    FROM plants WHERE {where}""",
                args,
            ).fetchall()
        return [
            {
                "round": r["round_num"],
                "t": r["t_ms"],
                "site": r["site"],
                "position": {"x": from_pos(r["x"]), "y": from_pos(r["y"])},
                "won": bool(r["won"]),
                "defused": bool(r["defused"]),
            }
            for r in rows
        ]

    def agent_breakdown(self, f: Filters) -> list[dict[str, Any]]:
        """Kills per agent for the current selection."""
        where, args = self._where(f)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT ka_id, COUNT(*) kills,
                           SUM((flags & {FLAG_TRADED}) != 0) traded
                    FROM kills WHERE {where} AND ka_id IS NOT NULL
                    GROUP BY ka_id ORDER BY kills DESC""",
                args,
            ).fetchall()
        agents = self._names("agent")
        return [
            {
                "agent": agents.get(r["ka_id"], "?"),
                "kills": r["kills"],
                "traded": r["traded"] or 0,
            }
            for r in rows
        ]

    def ability_breakdown(self, f: Filters) -> list[dict[str, Any]]:
        where, args = self._where(f)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT ability_id, ka_id, COUNT(*) kills
                    FROM kills WHERE {where} AND dmg_type = 1 AND ability_id IS NOT NULL
                    GROUP BY ability_id, ka_id ORDER BY kills DESC""",
                args,
            ).fetchall()
        abilities = self._names("ability")
        agents = self._names("agent")
        return [
            {
                "ability": abilities.get(r["ability_id"], "?"),
                "agent": agents.get(r["ka_id"], "?"),
                "kills": r["kills"],
            }
            for r in rows
        ]

    def weapon_breakdown(self, f: Filters) -> list[dict[str, Any]]:
        where, args = self._where(f)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT weapon_id, COUNT(*) kills
                    FROM kills WHERE {where} AND weapon_id IS NOT NULL
                    GROUP BY weapon_id ORDER BY kills DESC LIMIT 20""",
                args,
            ).fetchall()
        weapons = self._names("weapon")
        return [{"weapon": weapons.get(r["weapon_id"], "?"), "kills": r["kills"]} for r in rows]

    # --- personal stats -------------------------------------------------
    def player_summary(
        self, puuid: str, f: "Filters | None" = None
    ) -> dict[str, Any]:
        """Totals for one player, optionally within a filtered selection.

        Kills and deaths come from the same rows read from both ends, so
        a single pass answers both rather than two filtered queries.

        When `f` is given its clauses are applied, letting the tiles
        describe the current selection rather than only a career. The
        player and role fields on `f` are ignored here: this reads both
        ends deliberately, and constraining to one would make "deaths"
        always zero.
        """
        pid = self._ids("player").get(puuid)
        if pid is None:
            return {
                "kills": 0, "deaths": 0, "kd": 0.0, "matches": 0,
                "traded_deaths": 0, "trade_rate": 0.0,
                "first_bloods": 0, "first_deaths": 0,
                "opening_duels": 0, "opening_win_rate": 0.0,
                "trade_kills": 0, "untraded_deaths": 0,
                "rounds_won_with_kill": 0, "kill_round_win_rate": 0.0,
                "post_plant_kills": 0, "post_plant_deaths": 0,
                "multi_kill_rounds": 0, "best_round": 0,
                "supported_deaths": 0, "isolated_deaths": 0, "support_rate": 0.0,
                "crossfire_kills": 0,
                "advantage_deaths": 0, "advantage_rounds_thrown": 0, "advantage_throw_rate": 0.0,
                "clutch_kills": 0, "clutches_faced": 0, "clutches_won": 0, "clutch_win_rate": 0.0,
                "low_impact_kills": 0, "impact_kills": 0, "impact_kill_rate": 0.0,
                "tracked": False,
            }

        where, args = "1=1", []
        if f is not None:
            scoped = replace(f, player="", player_role="either")
            where, args = self._where(scoped, is_player=True, player_pid=pid)

        if where == "1=0":
            return {
                "kills": 0, "deaths": 0, "kd": 0.0, "matches": 0,
                "traded_deaths": 0, "trade_rate": 0.0,
                "first_bloods": 0, "first_deaths": 0,
                "opening_duels": 0, "opening_win_rate": 0.0,
                "trade_kills": 0, "untraded_deaths": 0,
                "rounds_won_with_kill": 0, "kill_round_win_rate": 0.0,
                "post_plant_kills": 0, "post_plant_deaths": 0,
                "multi_kill_rounds": 0, "best_round": 0,
                "supported_deaths": 0, "isolated_deaths": 0, "support_rate": 0.0,
                "crossfire_kills": 0,
                "advantage_deaths": 0, "advantage_rounds_thrown": 0, "advantage_throw_rate": 0.0,
                "clutch_kills": 0, "clutches_faced": 0, "clutches_won": 0, "clutch_win_rate": 0.0,
                "low_impact_kills": 0, "impact_kills": 0, "impact_kill_rate": 0.0,
                "tracked": True,
            }

        with self.db.connect() as conn:
            row = conn.execute(
                f"""SELECT
                        SUM(killer_pid = ?) kills,
                        SUM(victim_pid = ?) deaths,
                        SUM(victim_pid = ? AND (flags & {FLAG_TRADED}) != 0) traded_deaths,
                        SUM(killer_pid = ? AND (flags & {FLAG_FIRST_BLOOD}) != 0) first_bloods,
                        SUM(victim_pid = ? AND (flags & {FLAG_FIRST_BLOOD}) != 0) first_deaths,
                        SUM(killer_pid = ? AND (flags & {FLAG_TRADE_KILL}) != 0) trade_kills,
                        SUM(killer_pid = ? AND (flags & {FLAG_ROUND_WON}) != 0) kills_in_won,
                        SUM(killer_pid = ? AND (flags & {FLAG_POST_PLANT}) != 0) pp_kills,
                        SUM(victim_pid = ? AND (flags & {FLAG_POST_PLANT}) != 0) pp_deaths,
                        SUM(victim_pid = ? AND (flags & {FLAG_SUPPORTED}) != 0) supported_deaths,
                        SUM(victim_pid = ? AND (flags & {FLAG_ISOLATED}) != 0) isolated_deaths,
                        SUM(killer_pid = ? AND (flags & {FLAG_CROSSFIRE}) != 0) crossfire_kills,
                        SUM(victim_pid = ? AND (flags & {FLAG_ADVANTAGE_DEATH}) != 0) advantage_deaths,
                        SUM(killer_pid = ? AND (flags & {FLAG_CLUTCH_KILL}) != 0) clutch_kills,
                        SUM(killer_pid = ? AND (flags & {FLAG_LOW_IMPACT}) != 0) low_impact_kills,
                        COUNT(DISTINCT m) matches
                    FROM kills
                    WHERE (killer_pid = ? OR victim_pid = ?) AND {where}""",
                [pid] * 17 + args,
            ).fetchone()

            # Rounds where they got two or more kills. Grouped per round
            # within a match, since round numbers repeat across matches.
            multi = conn.execute(
                f"""SELECT COUNT(*) rounds, COALESCE(MAX(n), 0) best FROM (
                        SELECT COUNT(*) n FROM kills
                        WHERE killer_pid = ? AND {where}
                        GROUP BY m, round_num
                        HAVING n >= 2
                    )""",
                [pid] + args,
            ).fetchone()
            best = conn.execute(
                f"""SELECT COALESCE(MAX(n), 0) best FROM (
                        SELECT COUNT(*) n FROM kills
                        WHERE killer_pid = ? AND {where}
                        GROUP BY m, round_num
                    )""",
                [pid] + args,
            ).fetchone()

            # Clutches (1vX rounds)
            clutch_row = conn.execute(
                f"""SELECT
                        COUNT(DISTINCT m || ':' || round_num) total_clutches,
                        COUNT(DISTINCT CASE WHEN (flags & {FLAG_ROUND_WON}) != 0 THEN m || ':' || round_num END) won_clutches
                    FROM kills
                    WHERE killer_pid = ? AND (flags & {FLAG_CLUTCH_KILL}) != 0 AND {where}""",
                [pid] + args,
            ).fetchone()

            # Advantage rounds where this player was the casualty
            adv_row = conn.execute(
                f"""SELECT
                        COUNT(DISTINCT m || ':' || round_num) adv_deaths_rounds,
                        COUNT(DISTINCT CASE WHEN (flags & {FLAG_ROUND_WON}) = 0 THEN m || ':' || round_num END) adv_thrown_rounds
                    FROM kills
                    WHERE victim_pid = ? AND (flags & {FLAG_ADVANTAGE_DEATH}) != 0 AND {where}""",
                [pid] + args,
            ).fetchone()

        kills = row["kills"] or 0
        deaths = row["deaths"] or 0
        traded = row["traded_deaths"] or 0
        first_bloods = row["first_bloods"] or 0
        first_deaths = row["first_deaths"] or 0
        openings = first_bloods + first_deaths
        kills_in_won = row["kills_in_won"] or 0

        supported_deaths = row["supported_deaths"] or 0
        isolated_deaths = row["isolated_deaths"] or 0
        crossfire_kills = row["crossfire_kills"] or 0
        advantage_deaths = row["advantage_deaths"] or 0
        adv_deaths_rounds = adv_row["adv_deaths_rounds"] or 0
        adv_thrown_rounds = adv_row["adv_thrown_rounds"] or 0

        clutch_kills = row["clutch_kills"] or 0
        clutches_faced = clutch_row["total_clutches"] or 0
        clutches_won = clutch_row["won_clutches"] or 0

        low_impact_kills = row["low_impact_kills"] or 0
        impact_kills = max(0, kills - low_impact_kills)

        return {
            "kills": kills,
            "deaths": deaths,
            # Deaths can be zero in a small sample; report kills rather
            # than dividing by it.
            "kd": round(kills / deaths, 2) if deaths else float(kills),
            "matches": row["matches"] or 0,
            "traded_deaths": traded,
            # How often a team-mate answered your death: the one number
            # here that says something about the team, not the player.
            "trade_rate": round(traded / deaths, 4) if deaths else 0.0,
            "untraded_deaths": deaths - traded,
            "first_bloods": first_bloods,
            "first_deaths": first_deaths,
            # Opening duels are the ones you chose to take; winning them
            # is a different skill from overall K/D.
            "opening_duels": openings,
            "opening_win_rate": round(first_bloods / openings, 4) if openings else 0.0,
            "trade_kills": row["trade_kills"] or 0,
            # Of the rounds you got a kill in, how many did your team win.
            # Not "your win rate" -- it says whether your kills land in
            # rounds that matter.
            "rounds_won_with_kill": kills_in_won,
            "kill_round_win_rate": round(kills_in_won / kills, 4) if kills else 0.0,
            "post_plant_kills": row["pp_kills"] or 0,
            "post_plant_deaths": row["pp_deaths"] or 0,
            "multi_kill_rounds": multi["rounds"] or 0,
            "best_round": best["best"] or 0,
            # Tactical spacing & micro-positioning
            "supported_deaths": supported_deaths,
            "isolated_deaths": isolated_deaths,
            "support_rate": round(supported_deaths / deaths, 4) if deaths else 0.0,
            "crossfire_kills": crossfire_kills,
            # Discipline & Man-advantage
            "advantage_deaths": advantage_deaths,
            "advantage_rounds_thrown": adv_thrown_rounds,
            "advantage_throw_rate": round(adv_thrown_rounds / adv_deaths_rounds, 4) if adv_deaths_rounds else 0.0,
            # Clutches (1vX)
            "clutch_kills": clutch_kills,
            "clutches_faced": clutches_faced,
            "clutches_won": clutches_won,
            "clutch_win_rate": round(clutches_won / clutches_faced, 4) if clutches_faced else 0.0,
            # Impact vs Low Impact / Exit
            "low_impact_kills": low_impact_kills,
            "impact_kills": impact_kills,
            "impact_kill_rate": round(impact_kills / kills, 4) if kills else 0.0,
            "tracked": True,
        }

    def player_maps(self, puuid: str) -> list[dict[str, Any]]:
        """Per-map kills and deaths, for the player's map picker.

        Ordered by involvement, so the map they actually play is the one
        the tab opens on rather than whichever sorts first alphabetically.
        """
        pid = self._ids("player").get(puuid)
        if pid is None:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT map_id,
                          SUM(killer_pid = ?) kills,
                          SUM(victim_pid = ?) deaths,
                          COUNT(DISTINCT m) matches
                   FROM kills
                   WHERE killer_pid = ? OR victim_pid = ?
                   GROUP BY map_id""",
                [pid] * 4,
            ).fetchall()
        maps = self._names("map")
        out = [
            {
                "map_name": maps.get(r["map_id"], "?"),
                "kills": r["kills"] or 0,
                "deaths": r["deaths"] or 0,
                "matches": r["matches"] or 0,
            }
            for r in rows
        ]
        out.sort(key=lambda r: (-(r["kills"] + r["deaths"]), r["map_name"]))
        return out

    def player_matches(self, puuid: str, limit: int = 20) -> list[dict[str, Any]]:
        """A player's matches, newest first, with their line in each."""
        pid = self._ids("player").get(puuid)
        if pid is None:
            return []
        maps = self._names("map")
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT m.match_id, m.map_id, m.mode, m.queue, m.started_at,
                           m.rounds, m.avg_tier,
                           SUM(k.killer_pid = ?) kills,
                           SUM(k.victim_pid = ?) deaths,
                           SUM(k.killer_pid = ? AND (k.flags & {FLAG_FIRST_BLOOD}) != 0)
                               first_bloods,
                           MAX(CASE WHEN k.killer_pid = ? THEN k.ka_id
                                    WHEN k.victim_pid = ? THEN k.va_id END) agent_id
                    FROM kills k
                    JOIN matches m ON m.id = k.m
                    WHERE k.killer_pid = ? OR k.victim_pid = ?
                    GROUP BY k.m
                    ORDER BY m.started_at DESC
                    LIMIT ?""",
                [pid] * 7 + [limit],
            ).fetchall()
        agents = self._names("agent")
        out = []
        for r in rows:
            kills = r["kills"] or 0
            deaths = r["deaths"] or 0
            out.append(
                {
                    "match_id": r["match_id"],
                    "map_name": maps.get(r["map_id"], "?"),
                    "mode": r["mode"],
                    "queue": r["queue"],
                    "started_at": r["started_at"],
                    "rounds": r["rounds"],
                    "agent": agents.get(r["agent_id"], ""),
                    "kills": kills,
                    "deaths": deaths,
                    "kd": round(kills / deaths, 2) if deaths else float(kills),
                    "first_bloods": r["first_bloods"] or 0,
                }
            )
        return out

    def match_detail(self, match_id: str) -> dict[str, Any] | None:
        """Everything needed to review one match.

        Returns every kill with both endpoints, so the client can draw
        duel lines and filter by round or player without another request:
        a match is a few hundred kills, small enough to send whole.
        """
        row_id = self.db.match_row_id(match_id)
        if row_id is None:
            return None

        maps = self._names("map")
        agents = self._names("agent")
        weapons = self._names("weapon")
        abilities = self._names("ability")
        players = self._names("player")

        with self.db.connect() as conn:
            meta = conn.execute("SELECT * FROM matches WHERE id = ?", (row_id,)).fetchone()
            kill_rows = conn.execute(
                """SELECT round_num, t_ms, side, ka_id, va_id, weapon_id,
                          ability_id, dmg_type, vx, vy, kx, ky, flags,
                          killer_pid, victim_pid
                   FROM kills WHERE m = ? ORDER BY round_num, t_ms""",
                (row_id,),
            ).fetchall()
            plant_rows = conn.execute(
                """SELECT round_num, t_ms, site, x, y, won, defused
                   FROM plants WHERE m = ? ORDER BY round_num""",
                (row_id,),
            ).fetchall()

        kills = [
            {
                "round": r["round_num"],
                "t_ms": r["t_ms"],
                "side": SIDE_NAME.get(r["side"], "none"),
                "killer_agent": agents.get(r["ka_id"], ""),
                "victim_agent": agents.get(r["va_id"], ""),
                "killer": players.get(r["killer_pid"], ""),
                "victim": players.get(r["victim_pid"], ""),
                "weapon": weapons.get(r["weapon_id"], ""),
                "ability": abilities.get(r["ability_id"], ""),
                "damage_type": DMG_NAME.get(r["dmg_type"], "other"),
                "vx": from_pos(r["vx"]),
                "vy": from_pos(r["vy"]),
                "kx": from_pos(r["kx"]),
                "ky": from_pos(r["ky"]),
                "traded": bool(r["flags"] & FLAG_TRADED),
                "first_blood": bool(r["flags"] & FLAG_FIRST_BLOOD),
                "post_plant": bool(r["flags"] & FLAG_POST_PLANT),
                "round_won": bool(r["flags"] & FLAG_ROUND_WON),
            }
            for r in kill_rows
        ]

        # Per-player scoreboard, derived from the kills rather than stored
        # separately: the same rows already say who killed whom.
        board: dict[str, dict[str, Any]] = {}

        def entry(puuid: str, agent: str) -> dict[str, Any]:
            row = board.setdefault(
                puuid,
                {"puuid": puuid, "agent": agent, "kills": 0, "deaths": 0, "first_bloods": 0},
            )
            row["agent"] = row["agent"] or agent
            return row

        for k in kills:
            if k["killer"]:
                e = entry(k["killer"], k["killer_agent"])
                e["kills"] += 1
                if k["first_blood"]:
                    e["first_bloods"] += 1
            if k["victim"]:
                entry(k["victim"], k["victim_agent"])["deaths"] += 1

        # Look up team from rotations if available
        team_by_puuid: dict[str, str] = {}
        with self.db.connect() as conn:
            rot_teams = conn.execute(
                """SELECT player_pid, team FROM rotations
                   WHERE m = ? AND player_pid IS NOT NULL AND team IS NOT NULL
                   GROUP BY player_pid, team""",
                (row_id,),
            ).fetchall()
            player_names = self._names("player")
            for rt in rot_teams:
                p_u = player_names.get(rt["player_pid"])
                if p_u:
                    team_by_puuid[p_u] = (rt["team"] or "").capitalize()

        from .reference import get_agent
        for puuid_key, p_entry in board.items():
            ag_info = get_agent(p_entry["agent"])
            p_entry["role"] = ag_info.role if ag_info else ""
            p_entry["icon"] = ag_info.icon if ag_info else ""
            p_entry["team"] = team_by_puuid.get(puuid_key, "")

        scoreboard = sorted(board.values(), key=lambda p: (-p["kills"], p["deaths"]))
        for row in scoreboard:
            row["kd"] = (
                round(row["kills"] / row["deaths"], 2)
                if row["deaths"]
                else float(row["kills"])
            )

        return {
            "match_id": match_id,
            "map_name": maps.get(meta["map_id"], "?"),
            "mode": meta["mode"],
            "queue": meta["queue"],
            "started_at": meta["started_at"],
            "rounds": meta["rounds"],
            "avg_tier": meta["avg_tier"],
            "kills": kills,
            "plants": [
                {
                    "round": r["round_num"],
                    "t_ms": r["t_ms"],
                    "site": r["site"],
                    "x": from_pos(r["x"]),
                    "y": from_pos(r["y"]),
                    "won": bool(r["won"]),
                    "defused": bool(r["defused"]),
                }
                for r in plant_rows
            ],
            "scoreboard": scoreboard,
        }

    # --- macro rotation flow (6.3) ---------------------------------------
    def rotations(
        self,
        map_name: str | None = None,
        side: str = "defense",
        player: str | None = None,
        agent: str | None = None,
        match_id: str | None = None,
        team: str | None = None,
        round_num: int | None = None,
        min_count: int = 5,
        focus_zone: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate macro-rotation transitions between zones for a map, match, or player."""
        m_id = None
        if match_id:
            with self.db.connect() as conn:
                mrow = conn.execute(
                    "SELECT id, map_id FROM matches WHERE match_id = ?", (match_id,)
                ).fetchone()
                if mrow:
                    m_id = mrow["id"]
                    if not map_name:
                        map_names = self._names("map")
                        map_name = map_names.get(mrow["map_id"], "")

        if not map_name:
            map_name = "Ascent"

        maps_by_lower = {k.lower(): v for k, v in self._ids("map").items()}
        map_id = maps_by_lower.get(map_name.lower())
        if map_id is None:
            self._dims.pop("map", None)
            maps_by_lower = {k.lower(): v for k, v in self._ids("map").items()}
            map_id = maps_by_lower.get(map_name.lower())

        zones_dict = get_map_zones(map_name)

        if map_id is None or not zones_dict:
            return {
                "map_name": map_name,
                "side": side,
                "player": player,
                "agent": agent,
                "match_id": match_id,
                "team": team,
                "round_num": round_num,
                "available_agents": [],
                "match_players": [],
                "total_transitions": 0,
                "zones": list(zones_dict.values()) if zones_dict else [],
                "transitions": [],
            }

        # Side mapping: "attack" -> 1, "defense" -> 2, "all" -> 0
        side_l = (side or "defense").lower()
        side_id = 1 if side_l == "attack" else (2 if side_l == "defense" else 0)

        # Player resolution
        pid = None
        if player:
            if "#" in player:
                with self.db.connect() as conn:
                    parts = player.split("#", 1)
                    row = conn.execute(
                        "SELECT puuid, pid FROM tracked_players WHERE LOWER(name)=LOWER(?) AND LOWER(tag)=LOWER(?)",
                        (parts[0], parts[1]),
                    ).fetchone()
                    if row:
                        pid = row["pid"] or self._ids("player").get(row["puuid"])
                        if pid is None:
                            self._dims.pop("player", None)
                            pid = self._ids("player").get(row["puuid"])
                    if pid is None:
                        pid = self._ids("player").get(player) or self._ids("player").get(parts[0])
            else:
                pid = self._ids("player").get(player)
                if pid is None:
                    with self.db.connect() as conn:
                        row = conn.execute(
                            "SELECT puuid, pid FROM tracked_players WHERE LOWER(name)=LOWER(?)",
                            (player,),
                        ).fetchone()
                        if row:
                            pid = row["pid"] or self._ids("player").get(row["puuid"])
                if pid is None:
                    self._dims.pop("player", None)
                    pid = self._ids("player").get(player)

        with self.db.connect() as conn:
            # Check if rotations exist for this match, map, or player
            if not getattr(self.db, "read_only", False):
                if m_id is not None:
                    m_count = conn.execute(
                        "SELECT COUNT(*) as n FROM rotations WHERE m = ?", (m_id,)
                    ).fetchone()["n"]
                    if m_count == 0:
                        from .backfill_rotations import backfill_match_rotations
                        try:
                            backfill_match_rotations(self.db, conn, m_id, match_id, map_name)
                        except Exception:
                            pass
                else:
                    table_count = conn.execute(
                        "SELECT COUNT(*) as n FROM rotations WHERE map_id = ?", (map_id,)
                    ).fetchone()["n"]
                    if table_count == 0:
                        from .backfill_rotations import backfill_match_rotations
                        raw_rows = conn.execute(
                            "SELECT id, match_id FROM matches WHERE map_id = ? LIMIT 50", (map_id,)
                        ).fetchall()
                        for r in raw_rows:
                            try:
                                backfill_match_rotations(self.db, conn, r["id"], r["match_id"], map_name)
                            except Exception:
                                pass

                    if pid is not None:
                        # Ensure all matches featuring this player on this map are in rotations
                        missing_player_matches = conn.execute(
                            """SELECT DISTINCT m.id, m.match_id
                               FROM matches m
                               WHERE m.map_id = ?
                                 AND m.id IN (
                                     SELECT m FROM kills WHERE killer_pid = ? AND map_id = ?
                                     UNION
                                     SELECT m FROM kills WHERE victim_pid = ? AND map_id = ?
                                 )
                                 AND m.id NOT IN (
                                     SELECT DISTINCT m FROM rotations WHERE map_id = ?
                                 )""",
                            (map_id, pid, map_id, pid, map_id, map_id),
                        ).fetchall()
                        if missing_player_matches:
                            from .backfill_rotations import backfill_match_rotations
                            for r in missing_player_matches:
                                try:
                                    backfill_match_rotations(self.db, conn, r["id"], r["match_id"], map_name)
                                except Exception:
                                    pass

            # Agent resolution (after backfill, in case new agent was interned)
            agent_id = None
            if agent:
                agents_by_lower = {k.lower(): v for k, v in self._ids("agent").items()}
                agent_id = agents_by_lower.get(agent.lower())
                if agent_id is None:
                    self._dims.pop("agent", None)
                    agents_by_lower = {k.lower(): v for k, v in self._ids("agent").items()}
                    agent_id = agents_by_lower.get(agent.lower())

            where_clauses = ["map_id = ?"]
            params: list[Any] = [map_id]

            if m_id is not None:
                where_clauses.append("m = ?")
                params.append(m_id)

            if side_id != 0:
                where_clauses.append("side = ?")
                params.append(side_id)

            if player:
                if pid is not None:
                    where_clauses.append("player_pid = ?")
                    params.append(pid)
                else:
                    where_clauses.append("1=0")

            if agent:
                if agent_id is not None:
                    where_clauses.append("agent_id = ?")
                    params.append(agent_id)
                else:
                    where_clauses.append("1=0")

            if team:
                where_clauses.append("LOWER(team) = LOWER(?)")
                params.append(team)

            if round_num is not None:
                where_clauses.append("round_num = ?")
                params.append(int(round_num))

            if focus_zone:
                where_clauses.append("(from_zone = ? OR to_zone = ?)")
                params.extend([focus_zone, focus_zone])

            where_sql = " AND ".join(where_clauses)

            # In match, player, or agent mode, single transitions are relevant (default threshold 1)
            if m_id is not None or round_num is not None or player or agent:
                effective_min = max(1, int(min_count)) if min_count != 5 else 1
            else:
                effective_min = max(1, int(min_count))

            having_sql = "HAVING COUNT(*) >= ?"
            params.append(effective_min)

            sql = f"""
                SELECT
                    from_zone, to_zone,
                    COUNT(*) as count,
                    AVG((t_end_ms - t_start_ms) / 1000.0) as avg_duration_s,
                    SUM(won) as rounds_won
                FROM rotations
                WHERE {where_sql}
                GROUP BY from_zone, to_zone
                {having_sql}
                ORDER BY count DESC
            """

            rows = conn.execute(sql, params).fetchall()

            # Query available agents for player or match
            available_agents: list[str] = []
            agent_names = self._names("agent")
            if pid is not None:
                ag_rows = conn.execute(
                    """SELECT DISTINCT agent_id FROM (
                           SELECT agent_id FROM rotations WHERE map_id = ? AND player_pid = ? AND agent_id IS NOT NULL
                           UNION
                           SELECT ka_id as agent_id FROM kills WHERE map_id = ? AND killer_pid = ? AND ka_id IS NOT NULL
                           UNION
                           SELECT va_id as agent_id FROM kills WHERE map_id = ? AND victim_pid = ? AND va_id IS NOT NULL
                       )""",
                    (map_id, pid, map_id, pid, map_id, pid),
                ).fetchall()
                if any(r["agent_id"] not in agent_names for r in ag_rows):
                    self._dims.pop("~agent", None)
                    agent_names = self._names("agent")
                available_agents = sorted(filter(None, [agent_names.get(r["agent_id"]) for r in ag_rows]))
            elif m_id is not None:
                ag_rows = conn.execute(
                    "SELECT DISTINCT agent_id FROM rotations WHERE m = ? AND agent_id IS NOT NULL",
                    (m_id,),
                ).fetchall()
                if any(r["agent_id"] not in agent_names for r in ag_rows):
                    self._dims.pop("~agent", None)
                    agent_names = self._names("agent")
                available_agents = sorted(filter(None, [agent_names.get(r["agent_id"]) for r in ag_rows]))

            # Query match players if match is scoped
            match_players: list[dict[str, Any]] = []
            if m_id is not None:
                from .reference import get_agent
                p_rows = conn.execute(
                    """SELECT DISTINCT player_pid, agent_id, team
                       FROM rotations
                       WHERE m = ? AND player_pid IS NOT NULL
                       ORDER BY CASE WHEN LOWER(team)='blue' THEN 0 ELSE 1 END, team, agent_id""",
                    (m_id,),
                ).fetchall()
                player_names = self._names("player")
                agent_names = self._names("agent")

                val_conn = None
                try:
                    from .paths import DATA_DIR
                    val_db_path = DATA_DIR / "valheatmap.db"
                    if val_db_path.exists():
                        val_conn = sqlite3.connect(f"file:{val_db_path}?mode=ro", uri=True)
                except Exception:
                    val_conn = None

                for pr in p_rows:
                    p_pid = pr["player_pid"]
                    a_id = pr["agent_id"]
                    t_str = (pr["team"] or "").capitalize()
                    puuid_str = player_names.get(p_pid, "")
                    agent_str = agent_names.get(a_id, "")
                    ag_info = get_agent(agent_str)

                    disp_name = ""
                    t_row = conn.execute(
                        "SELECT name, tag FROM tracked_players WHERE puuid = ?", (puuid_str,)
                    ).fetchone()
                    if t_row and t_row["name"] and t_row["name"] != "Unknown":
                        disp_name = f"{t_row['name']}#{t_row['tag']}" if t_row["tag"] else t_row["name"]
                    elif val_conn:
                        try:
                            v_row = val_conn.execute(
                                "SELECT name, tag FROM players WHERE puuid = ?", (puuid_str,)
                            ).fetchone()
                            if v_row and v_row[0] and v_row[0] != "Unknown":
                                disp_name = f"{v_row[0]}#{v_row[1]}" if v_row[1] else v_row[0]
                        except Exception:
                            pass

                    match_players.append({
                        "puuid": puuid_str,
                        "name": disp_name,
                        "agent": agent_str,
                        "role": ag_info.role if ag_info else "",
                        "icon": ag_info.icon if ag_info else "",
                        "team": t_str,
                    })

                if val_conn:
                    try:
                        val_conn.close()
                    except Exception:
                        pass

            from .reference import get_agent

            # Query agent breakdown per transition
            agent_rot_sql = f"""
                SELECT
                    from_zone, to_zone, agent_id,
                    COUNT(*) as cnt
                FROM rotations
                WHERE {where_sql} AND agent_id IS NOT NULL
                GROUP BY from_zone, to_zone, agent_id
                ORDER BY cnt DESC
            """
            ag_trans_rows = conn.execute(agent_rot_sql, params[:-1]).fetchall()
            agents_by_route: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for atr in ag_trans_rows:
                ag_name = agent_names.get(atr["agent_id"])
                if not ag_name:
                    continue
                ag_inf = get_agent(ag_name)
                key = (atr["from_zone"], atr["to_zone"])
                agents_by_route.setdefault(key, []).append({
                    "agent": ag_name,
                    "icon": ag_inf.icon if ag_inf else "",
                    "role": ag_inf.role if ag_inf else "",
                    "count": atr["cnt"],
                })

            # Query round setups if match and round are specified
            round_setups: list[dict[str, Any]] = []
            if m_id is not None and round_num is not None:
                round_setup_rows = conn.execute(
                    """SELECT player_pid, agent_id, team, from_zone, to_zone, t_start_ms, won
                       FROM rotations
                       WHERE m = ? AND round_num = ? AND player_pid IS NOT NULL
                       ORDER BY team, t_start_ms ASC""",
                    (m_id, int(round_num)),
                ).fetchall()
                for rsr in round_setup_rows:
                    ag_name = agent_names.get(rsr["agent_id"], "")
                    ag_inf = get_agent(ag_name)
                    round_setups.append({
                        "team": (rsr["team"] or "").capitalize(),
                        "agent": ag_name,
                        "icon": ag_inf.icon if ag_inf else "",
                        "role": ag_inf.role if ag_inf else "",
                        "from_zone": rsr["from_zone"],
                        "to_zone": rsr["to_zone"],
                        "start_s": round(rsr["t_start_ms"] / 1000.0, 1),
                        "won": bool(rsr["won"]),
                    })

        # Compute zone traffic and departure/arrival shares
        outgoing_totals: dict[str, int] = {}
        incoming_totals: dict[str, int] = {}
        transitions: list[dict[str, Any]] = []

        for r in rows:
            fz = r["from_zone"]
            tz = r["to_zone"]
            c = r["count"]
            outgoing_totals[fz] = outgoing_totals.get(fz, 0) + c
            incoming_totals[tz] = incoming_totals.get(tz, 0) + c

        for r in rows:
            fz = r["from_zone"]
            tz = r["to_zone"]
            c = r["count"]
            out_tot = outgoing_totals.get(fz, 0)
            in_tot = incoming_totals.get(tz, 0)
            won_cnt = r["rounds_won"] or 0
            route_agents = agents_by_route.get((fz, tz), [])

            transitions.append(
                {
                    "from_zone": fz,
                    "to_zone": tz,
                    "count": c,
                    "outgoing_share": round(c / out_tot, 4) if out_tot else 0.0,
                    "incoming_share": round(c / in_tot, 4) if in_tot else 0.0,
                    "avg_duration_s": round(r["avg_duration_s"] or 0.0, 1),
                    "rounds_won": won_cnt,
                    "win_rate": round(won_cnt / c, 4) if c else 0.0,
                    "agents": route_agents,
                }
            )

        # Update zone traffic
        zone_list = []
        for zid, zinfo in zones_dict.items():
            tot = outgoing_totals.get(zid, 0) + incoming_totals.get(zid, 0)
            zone_list.append(
                {
                    **zinfo,
                    "outgoing_count": outgoing_totals.get(zid, 0),
                    "incoming_count": incoming_totals.get(zid, 0),
                    "total_traffic": tot,
                }
            )

        return {
            "map_name": map_name,
            "side": side,
            "player": player,
            "agent": agent,
            "match_id": match_id,
            "team": team,
            "round_num": round_num,
            "available_agents": available_agents,
            "match_players": match_players,
            "round_setups": round_setups,
            "total_transitions": sum(t["count"] for t in transitions),
            "zones": zone_list,
            "transitions": transitions,
        }
