"""Derived stats that mainstream Valorant trackers don't surface.

Everything here is computed from spatial + temporal kill context rather than
from the box score, which is what makes it different from tracker.gg et al.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Sequence

from ..models import DamageType, Match, Side
from ..reference import MapInfo, get_agent
from .kills import EnrichedKill

# Opening duels inside this window are the "first contact" of a round.
OPENING_WINDOW_MS = 30_000
# Grid used for map-control / hot-zone aggregation, in minimap units.
ZONE_GRID = 12


def trade_report(kills: Sequence[EnrichedKill], match: Match) -> dict[str, Any]:
    """Per-player trade economy: who gets avenged, who does the avenging.

    `traded_death_rate` is the share of a player's deaths that teammates
    traded back -- a proxy for whether they fight with support. Low values
    flag players who die in isolation.
    """
    deaths: dict[str, int] = defaultdict(int)
    traded_deaths: dict[str, int] = defaultdict(int)
    trade_kills: dict[str, int] = defaultdict(int)
    kills_given: dict[str, int] = defaultdict(int)
    untraded_kills: dict[str, int] = defaultdict(int)

    for ek in kills:
        k = ek.kill
        deaths[k.victim_puuid] += 1
        kills_given[k.killer_puuid] += 1
        if ek.traded:
            traded_deaths[k.victim_puuid] += 1
        else:
            # The killer got away with it: an unpunished entry.
            untraded_kills[k.killer_puuid] += 1
        if ek.trade_kill:
            trade_kills[k.killer_puuid] += 1

    rows = []
    for p in match.players:
        d = deaths.get(p.puuid, 0)
        kg = kills_given.get(p.puuid, 0)
        rows.append(
            {
                "puuid": p.puuid,
                "name": p.display,
                "agent": p.agent,
                "team": p.team,
                "kills": kg,
                "deaths": d,
                "traded_deaths": traded_deaths.get(p.puuid, 0),
                "traded_death_rate": round(traded_deaths.get(p.puuid, 0) / d, 4) if d else 0.0,
                "trade_kills": trade_kills.get(p.puuid, 0),
                "untraded_kills": untraded_kills.get(p.puuid, 0),
                "untraded_kill_rate": round(untraded_kills.get(p.puuid, 0) / kg, 4) if kg else 0.0,
            }
        )
    rows.sort(key=lambda r: (-r["kills"], r["deaths"]))
    return {"players": rows}


def opening_duels(kills: Sequence[EnrichedKill], match: Match) -> dict[str, Any]:
    """Who takes -- and wins -- the first fight of each round.

    Opening duels swing rounds far more than raw K/D, but trackers bury
    them. Win rate here is per-player: opening kills / opening duels taken.
    """
    wins: dict[str, int] = defaultdict(int)
    losses: dict[str, int] = defaultdict(int)
    round_won_after_ok: dict[str, int] = defaultdict(int)

    for ek in kills:
        if not ek.first_blood:
            continue
        wins[ek.kill.killer_puuid] += 1
        losses[ek.kill.victim_puuid] += 1
        if ek.round_won:
            round_won_after_ok[ek.kill.killer_puuid] += 1

    rows = []
    for p in match.players:
        w, l = wins.get(p.puuid, 0), losses.get(p.puuid, 0)
        total = w + l
        if total == 0:
            continue
        rows.append(
            {
                "puuid": p.puuid,
                "name": p.display,
                "agent": p.agent,
                "team": p.team,
                "opening_kills": w,
                "opening_deaths": l,
                "duels": total,
                "win_rate": round(w / total, 4),
                # Converting an opening kill into a round win is the real
                # measure of whether the aggression paid off.
                "round_conversion": round(round_won_after_ok.get(p.puuid, 0) / w, 4) if w else 0.0,
            }
        )
    rows.sort(key=lambda r: (-r["duels"], -r["win_rate"]))
    return {"players": rows}


def utility_report(kills: Sequence[EnrichedKill], match: Match) -> dict[str, Any]:
    """Which abilities actually convert into kills, by agent and ability."""
    by_ability: dict[tuple[str, str, str], int] = defaultdict(int)
    for ek in kills:
        k = ek.kill
        if k.damage_type is not DamageType.ABILITY:
            continue
        label = k.ability_name or (k.ability_slot.value if k.ability_slot else "Ability")
        by_ability[(ek.killer_agent, label, k.ability_slot.value if k.ability_slot else "")] += 1

    rows = []
    for (agent, ability, slot), count in by_ability.items():
        info = get_agent(agent)
        rows.append(
            {
                "agent": agent,
                "agent_icon": info.icon if info else "",
                "ability": ability,
                "slot": slot,
                "kills": count,
            }
        )
    rows.sort(key=lambda r: -r["kills"])
    total_util = sum(r["kills"] for r in rows)
    return {
        "abilities": rows,
        "total_utility_kills": total_util,
        "utility_share": round(total_util / len(kills), 4) if kills else 0.0,
    }


def weapon_report(kills: Sequence[EnrichedKill]) -> dict[str, Any]:
    by_weapon: dict[str, dict[str, Any]] = {}
    for ek in kills:
        k = ek.kill
        if k.damage_type is not DamageType.WEAPON or not k.weapon_name:
            continue
        row = by_weapon.setdefault(
            k.weapon_name, {"weapon": k.weapon_name, "kills": 0, "traded": 0, "distance_sum": 0.0, "ranged": 0}
        )
        row["kills"] += 1
        if ek.traded:
            row["traded"] += 1
        if k.killer_location is not None and k.victim_location is not None:
            row["distance_sum"] += math.hypot(
                k.killer_location.x - k.victim_location.x,
                k.killer_location.y - k.victim_location.y,
            )
            row["ranged"] += 1
    rows = []
    for row in by_weapon.values():
        ranged = row.pop("ranged")
        dist = row.pop("distance_sum")
        # World units -> metres: Valorant uses ~100 units per metre.
        row["avg_distance_m"] = round(dist / ranged / 100, 1) if ranged else None
        rows.append(row)
    rows.sort(key=lambda r: -r["kills"])
    return {"weapons": rows}


def duel_distance(kills: Sequence[EnrichedKill]) -> dict[str, Any]:
    """Distribution of engagement ranges -- who fights long vs close."""
    buckets = {"<5m": 0, "5-10m": 0, "10-20m": 0, "20-30m": 0, "30m+": 0}
    values: list[float] = []
    for ek in kills:
        k = ek.kill
        if k.killer_location is None or k.victim_location is None:
            continue
        d = math.hypot(
            k.killer_location.x - k.victim_location.x,
            k.killer_location.y - k.victim_location.y,
        ) / 100.0
        values.append(d)
        if d < 5:
            buckets["<5m"] += 1
        elif d < 10:
            buckets["5-10m"] += 1
        elif d < 20:
            buckets["10-20m"] += 1
        elif d < 30:
            buckets["20-30m"] += 1
        else:
            buckets["30m+"] += 1
    values.sort()
    median = values[len(values) // 2] if values else 0.0
    return {
        "buckets": [{"range": k, "count": v} for k, v in buckets.items()],
        "median_m": round(median, 1),
        "sample": len(values),
    }


def hot_zones(
    kills: Sequence[EnrichedKill],
    map_info: MapInfo,
    grid: int = ZONE_GRID,
    anchor: str = "victim",
) -> dict[str, Any]:
    """Grid the map and score each cell by who wins fights there.

    A cell where attackers consistently get kills is attacker-favoured
    ground. This is the closest thing to a "map control" stat that kill
    data alone can support.
    """
    cells: dict[tuple[int, int], dict[str, Any]] = {}
    for ek in kills:
        k = ek.kill
        loc = k.victim_location if anchor == "victim" else k.killer_location
        if loc is None:
            continue
        mx, my = map_info.to_minimap(loc.x, loc.y)
        if not (0.0 <= mx <= 1.0 and 0.0 <= my <= 1.0):
            continue
        cx, cy = min(int(mx * grid), grid - 1), min(int(my * grid), grid - 1)
        cell = cells.setdefault(
            (cx, cy), {"x": cx, "y": cy, "kills": 0, "attack": 0, "defense": 0, "traded": 0}
        )
        cell["kills"] += 1
        if k.killer_side is Side.ATTACK:
            cell["attack"] += 1
        elif k.killer_side is Side.DEFENSE:
            cell["defense"] += 1
        if ek.traded:
            cell["traded"] += 1

    out = []
    for cell in cells.values():
        sided = cell["attack"] + cell["defense"]
        cell["attack_share"] = round(cell["attack"] / sided, 4) if sided else None
        cell["trade_rate"] = round(cell["traded"] / cell["kills"], 4) if cell["kills"] else 0.0
        cell["cell_size"] = 1 / grid
        out.append(cell)
    out.sort(key=lambda c: -c["kills"])
    return {"grid": grid, "cells": out}


def timing_profile(kills: Sequence[EnrichedKill], bucket_ms: int = 10_000) -> dict[str, Any]:
    """When in the round do kills happen, split by side.

    Shows execute timings and default-round rhythms -- e.g. a spike in
    attacker kills at 35-45s is a team that likes late executes.
    """
    buckets: dict[int, dict[str, int]] = {}
    for ek in kills:
        b = (ek.kill.time_in_round_ms // bucket_ms) * bucket_ms
        row = buckets.setdefault(b, {"t": b, "attack": 0, "defense": 0, "total": 0})
        row["total"] += 1
        if ek.kill.killer_side is Side.ATTACK:
            row["attack"] += 1
        elif ek.kill.killer_side is Side.DEFENSE:
            row["defense"] += 1
    return {"bucket_ms": bucket_ms, "buckets": [buckets[k] for k in sorted(buckets)]}


def multikill_rounds(kills: Sequence[EnrichedKill], match: Match) -> dict[str, Any]:
    """Rounds where one player got 3+ kills, with where those kills landed."""
    per_round: dict[tuple[int, str], list[EnrichedKill]] = defaultdict(list)
    for ek in kills:
        per_round[(ek.kill.round_num, ek.kill.killer_puuid)].append(ek)
    rows = []
    for (rnd, puuid), group in per_round.items():
        if len(group) < 3:
            continue
        player = match.player(puuid)
        rows.append(
            {
                "round": rnd,
                "puuid": puuid,
                "name": player.display if player else "",
                "agent": player.agent if player else "",
                "kills": len(group),
                "won_round": group[0].round_won,
                "span_ms": max(g.kill.time_in_round_ms for g in group)
                - min(g.kill.time_in_round_ms for g in group),
            }
        )
    rows.sort(key=lambda r: (-r["kills"], r["round"]))
    return {"rounds": rows}


def economy_of_death(kills: Sequence[EnrichedKill], match: Match) -> dict[str, Any]:
    """Round win rate conditioned on winning the opening duel."""
    rounds_with_ok = {}
    for ek in kills:
        if ek.first_blood:
            rounds_with_ok[ek.kill.round_num] = ek
    if not rounds_with_ok:
        return {"sample": 0, "win_rate_after_opening_kill": 0.0}
    won = sum(1 for ek in rounds_with_ok.values() if ek.round_won)
    atk = [ek for ek in rounds_with_ok.values() if ek.kill.killer_side is Side.ATTACK]
    dfn = [ek for ek in rounds_with_ok.values() if ek.kill.killer_side is Side.DEFENSE]
    return {
        "sample": len(rounds_with_ok),
        "win_rate_after_opening_kill": round(won / len(rounds_with_ok), 4),
        "attack_sample": len(atk),
        "attack_win_rate": round(sum(1 for e in atk if e.round_won) / len(atk), 4) if atk else 0.0,
        "defense_sample": len(dfn),
        "defense_win_rate": round(sum(1 for e in dfn if e.round_won) / len(dfn), 4) if dfn else 0.0,
    }
