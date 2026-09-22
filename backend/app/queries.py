"""Query layer over the analytics database.

Builds parameterised SQL from the UI's filters. Every query is anchored on
`map_id` because that is how the indexes are ordered and how users actually
navigate: pick a map first, then narrow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

from .analytics_db import (
    DMG_NAME,
    POS_SCALE,
    from_pos,
    FLAG_FIRST_BLOOD,
    FLAG_POST_PLANT,
    FLAG_ROUND_WON,
    FLAG_TRADE_KILL,
    FLAG_TRADED,
    SIDE_NAME,
    AnalyticsDB,
)

# Returning every point is wasteful: the heatmap bins them anyway, and the
# payload has to cross the network. Above this we sample deterministically.
MAX_POINTS = 40_000

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

    def _ids(self, kind: str) -> dict[str, int]:
        if kind not in self._dims:
            self._dims[kind] = self.db.dim_ids(kind)
        return self._dims[kind]

    def _names(self, kind: str) -> dict[int, str]:
        key = f"~{kind}"
        if key not in self._dims:
            self._dims[key] = self.db.dim_names(kind)  # type: ignore[assignment]
        return self._dims[key]  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Drop cached dimension maps after an ingest adds new names."""
        self._dims.clear()

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
    def _where(self, f: Filters, table: str = "kills") -> tuple[str, list[Any]]:
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

        if f.player and table == "kills":
            pid = self._ids("player").get(f.player)
            if pid is None:
                # Never crawled, or crawled before attribution existed.
                # Empty is the honest answer; a full scan is not.
                return "1=0", []
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
            if f.agents:
                ids = self._resolve("agent", f.agents)
                if ids is None:
                    return "1=0", []
                clauses.append(f"ka_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            if f.victim_agents:
                ids = self._resolve("agent", f.victim_agents)
                if ids is None:
                    return "1=0", []
                clauses.append(f"va_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            # Roles narrow the same columns as the agent filters, so
            # selecting Duelist *and* Jett means Jett, not both sets.
            if f.roles:
                ids = self._role_agent_ids(f.roles)
                if ids is None:
                    return "1=0", []
                clauses.append(f"ka_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            if f.victim_roles:
                ids = self._role_agent_ids(f.victim_roles)
                if ids is None:
                    return "1=0", []
                clauses.append(f"va_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            if f.weapons:
                ids = self._resolve("weapon", f.weapons)
                if ids is None:
                    return "1=0", []
                clauses.append(f"weapon_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
            if f.abilities:
                ids = self._resolve("ability", f.abilities)
                if ids is None:
                    return "1=0", []
                clauses.append(f"ability_id IN ({','.join('?' * len(ids))})")
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
                sample_sql = f" AND (rowid % {stride}) = 0"

            rows = conn.execute(
                f"""SELECT t_ms, side, ka_id, va_id, weapon_id, ability_id, dmg_type,
                           vx, vy, kx, ky, flags, round_num, killer_pid, victim_pid
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
        return {
            "points": points,
            "total": total,
            "sampled": len(points) < total,
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
                "tracked": False,
            }

        where, args = "1=1", []
        if f is not None:
            scoped = replace(f, player="", player_role="killer")
            where, args = self._where(scoped)

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
                        COUNT(DISTINCT m) matches
                    FROM kills
                    WHERE (killer_pid = ? OR victim_pid = ?) AND {where}""",
                [pid] * 11 + args,
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

        kills = row["kills"] or 0
        deaths = row["deaths"] or 0
        traded = row["traded_deaths"] or 0
        first_bloods = row["first_bloods"] or 0
        first_deaths = row["first_deaths"] or 0
        openings = first_bloods + first_deaths
        kills_in_won = row["kills_in_won"] or 0
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
