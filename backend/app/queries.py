"""Query layer over the analytics database.

Builds parameterised SQL from the UI's filters. Every query is anchored on
`map_id` because that is how the indexes are ordered and how users actually
navigate: pick a map first, then narrow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .analytics_db import (
    DMG_NAME,
    FLAG_FIRST_BLOOD,
    FLAG_POST_PLANT,
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


@dataclass(slots=True)
class Filters:
    map_name: str = ""
    acts: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    victim_agents: list[str] = field(default_factory=list)
    weapons: list[str] = field(default_factory=list)
    sides: list[str] = field(default_factory=list)
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
            sides=lst("sides"),
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
            if f.weapons:
                ids = self._resolve("weapon", f.weapons)
                if ids is None:
                    return "1=0", []
                clauses.append(f"weapon_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)
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
                           vx, vy, kx, ky, flags, round_num
                    FROM kills WHERE {where}{sample_sql}""",
                args,
            ).fetchall()

        agents = self._names("agent")
        weapons = self._names("weapon")
        abilities = self._names("ability")
        points = [
            {
                "t": r["t_ms"],
                "round": r["round_num"],
                "side": SIDE_NAME.get(r["side"], "none"),
                "killer_agent": agents.get(r["ka_id"], ""),
                "victim_agent": agents.get(r["va_id"], ""),
                "weapon": weapons.get(r["weapon_id"], ""),
                "ability": abilities.get(r["ability_id"], ""),
                "type": DMG_NAME.get(r["dmg_type"], "other"),
                "victim_pos": {"x": r["vx"], "y": r["vy"]},
                "killer_pos": (
                    {"x": r["kx"], "y": r["ky"]} if r["kx"] is not None else None
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
                "position": {"x": r["x"], "y": r["y"]},
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
