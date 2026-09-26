"""Macro Rotation Flow & Transition Graph Analytics (Module 6.3).

Tracks tactical movement across macro zones between discrete events in a match.
Reconstructs player rotation corridors, transit velocities, and round conversion rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence

from ..models import Match, Side
from ..reference import MapInfo, get_map


def macro_zone_for_callout(map_name: str, super_reg: str, reg: str) -> str:
    """Map an official Riot callout to a clean, canonical macro zone."""
    map_l = map_name.lower()
    if super_reg == "Defender Side":
        return "CT Spawn"
    if super_reg == "Attacker Side":
        return "T Spawn"

    # Ascent
    if map_l == "ascent":
        if super_reg == "A" and reg in {"Tree", "Garden"}:
            return "A Tree"
        if super_reg == "Mid" and reg in {"Market", "Link"}:
            return "Market"
        if super_reg == "A" and reg in {"Window", "Rafters"}:
            return "A Heaven"
        if super_reg == "A" and reg in {"Site", "Wine"}:
            return "A Site"
        if super_reg == "A" and reg in {"Main", "Lobby"}:
            return "A Main"
        if super_reg == "B" and reg in {"Site", "Boat House"}:
            return "B Site"
        if super_reg == "B" and reg in {"Main", "Lobby"}:
            return "B Main"
        if super_reg == "Mid":
            return "Mid"

    # Haven
    if map_l == "haven":
        if super_reg == "A" and reg in {"Tower"}:
            return "A Heaven"
        if super_reg == "A" and reg in {"Site", "Sewer"}:
            return "A Site"
        if super_reg == "A" and reg in {"Long", "Lobby", "Garden"}:
            return "A Long"
        if super_reg == "B":
            return "B Site"
        if super_reg == "C" and reg in {"Garage", "Doors", "Window"}:
            return "Garage"
        if super_reg == "C" and reg in {"Site"}:
            return "C Site"
        if super_reg == "C" and reg in {"Long", "Lobby", "Cubby"}:
            return "C Long"
        if super_reg == "Mid":
            return "Mid"

    # Bind
    if map_l == "bind":
        if super_reg == "A" and reg in {"Tower", "Lamps"}:
            return "A Tower"
        if super_reg == "A" and reg in {"Site", "Bath"}:
            return "A Site"
        if super_reg == "A" and reg in {"Short", "Lobby", "Cubby", "Exit", "Teleporter", "Link"}:
            return "A Short"
        if super_reg == "B" and reg in {"Window", "Garden"}:
            return "B Hookah"
        if super_reg == "B" and reg in {"Site", "Elbow", "Hall"}:
            return "B Site"
        if super_reg == "B" and reg in {"Long", "Fountain", "Exit", "Teleporter", "Link", "Short"}:
            return "B Long"

    # Split
    if map_l == "split":
        if super_reg == "A" and reg in {"Tower", "Rafters"}:
            return "A Heaven"
        if super_reg == "A" and reg in {"Site", "Screens", "Back"}:
            return "A Site"
        if super_reg == "A" and reg in {"Main", "Lobby", "Ramps", "Sewer"}:
            return "A Main"
        if super_reg == "Mid" and reg in {"Mail"}:
            return "Mail"
        if super_reg == "Mid" and reg in {"Vent"}:
            return "Vents"
        if super_reg == "Mid":
            return "Mid"
        if super_reg == "B" and reg in {"Tower", "Rafters"}:
            return "B Heaven"
        if super_reg == "B" and reg in {"Site", "Back", "Alley"}:
            return "B Site"
        if super_reg == "B" and reg in {"Garage", "Main", "Lobby", "Stairs", "Link"}:
            return "B Main"

    # Lotus
    if map_l == "lotus":
        if super_reg == "A" and reg in {"Tree", "Hut", "Door"}:
            return "A Tree"
        if super_reg == "A" and reg in {"Site", "Drop", "Root", "Stairs", "Top"}:
            return "A Site"
        if super_reg == "A" and reg in {"Main", "Lobby", "Rubble", "Link"}:
            return "A Main"
        if super_reg == "B":
            return "B Site"
        if super_reg == "C" and reg in {"Hall", "Door", "Link"}:
            return "C Connector"
        if super_reg == "C" and reg in {"Site", "Bend", "Gravel", "Mound", "Waterfall"}:
            return "C Site"
        if super_reg == "C" and reg in {"Main", "Lobby"}:
            return "C Main"

    # Icebox
    if map_l == "icebox":
        if super_reg == "A" and reg in {"Rafters", "Screen", "Pipes"}:
            return "A Screen"
        if super_reg == "A" and reg in {"Site", "Nest"}:
            return "A Site"
        if super_reg == "A" and reg in {"Belt", "Main"}:
            return "A Belt"
        if super_reg == "B" and reg in {"Kitchen", "Tube"}:
            return "Kitchen"
        if super_reg == "B" and reg in {
            "Site", "Snowman", "Yellow", "Back", "Snow Pile", "Green", "Orange", "Cubby", "Fence"
        }:
            return "B Site"
        if super_reg == "B" and reg in {"Garage", "Hall"}:
            return "B Garage"
        if super_reg == "Mid" or reg in {"Boiler", "Pallet", "Blue"}:
            return "Mid"

    # Sunset
    if map_l == "sunset":
        if super_reg == "A" and reg in {"Site", "Alley", "Elbow"}:
            return "A Site"
        if super_reg == "A" and reg in {"Main", "Lobby"}:
            return "A Main"
        if super_reg == "A" and reg in {"Link"}:
            return "A Link"
        if super_reg == "B" and reg in {"Site"}:
            return "B Site"
        if super_reg == "B" and reg in {"Main", "Lobby"}:
            return "B Main"
        if reg in {"Boba"}:
            return "Boba"
        if reg in {"Market"}:
            return "Market"
        if super_reg == "Mid":
            return "Mid"

    # General Fallback
    if reg in {"Tower", "Heaven", "Rafters"}:
        return f"{super_reg} Heaven"
    if reg in {"Tree", "Garden"}:
        return f"{super_reg} Tree"
    if reg in {"Market", "Boba", "Mail", "Kitchen"}:
        return reg
    if reg in {"Garage"}:
        return f"{super_reg} Garage"
    if "Site" in reg or reg in {
        "Site", "Back", "Snowman", "Yellow", "Nest", "Wine", "Pillars", "Hut", "Pyramids", "Screen"
    }:
        return f"{super_reg} Site"
    if "Main" in reg or "Lobby" in reg or "Long" in reg or "Belt" in reg or "Ramp" in reg:
        return f"{super_reg} Main"
    if super_reg == "Mid":
        return "Mid"
    return f"{super_reg} {reg}"


@lru_cache(maxsize=32)
def get_map_zones(map_name: str) -> dict[str, dict[str, Any]]:
    """Return dict of macro zone definitions for a map with normalized minimap coordinates."""
    m = get_map(map_name)
    if not m or not m.callouts:
        return {}

    zone_coords: dict[str, list[tuple[float, float]]] = {}
    for c in m.callouts:
        z = macro_zone_for_callout(map_name, c.get("superRegionName", ""), c.get("regionName", ""))
        loc = c.get("location", {})
        mx, my = m.to_minimap(loc.get("x", 0.0), loc.get("y", 0.0))
        zone_coords.setdefault(z, []).append((mx, my))

    zones: dict[str, dict[str, Any]] = {}
    for z, pts in zone_coords.items():
        avg_x = sum(p[0] for p in pts) / len(pts)
        avg_y = sum(p[1] for p in pts) / len(pts)
        zones[z] = {
            "id": z,
            "name": z,
            "x": round(avg_x, 4),
            "y": round(avg_y, 4),
            "callout_count": len(pts),
        }
    return zones


def resolve_zone_from_callouts(
    map_name: str, x: float, y: float, callout_tuples: Sequence[tuple[str, float, float]]
) -> str:
    """Fast nearest-callout resolver mapped to a macro zone."""
    best_dist = float("inf")
    best_zone = "Unknown"
    for zone_name, cx, cy in callout_tuples:
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        if d2 < best_dist:
            best_dist = d2
            best_zone = zone_name
    return best_zone


@dataclass(slots=True)
class RotationEvent:
    t_ms: int
    x: float
    y: float


@dataclass(slots=True)
class ExtractedRotation:
    round_num: int
    side: int  # 1 attack, 2 defense
    player_puuid: str
    from_zone: str
    to_zone: str
    t_start_ms: int
    t_end_ms: int
    won: int  # 1 won, 0 lost
    agent: str = ""
    team: str = ""


def extract_rotations(match: Match, map_info: MapInfo) -> list[ExtractedRotation]:
    """Extract macro-rotation transitions for all players across all rounds of a match."""
    if not map_info or not map_info.callouts:
        return []

    map_name = match.meta.map_name
    # Pre-resolve each callout to its macro zone and world coordinates
    callout_tuples = [
        (
            macro_zone_for_callout(map_name, c.get("superRegionName", ""), c.get("regionName", "")),
            c.get("location", {}).get("x", 0.0),
            c.get("location", {}).get("y", 0.0),
        )
        for c in map_info.callouts
    ]

    puuid_team = {p.puuid: p.team for p in match.players}
    puuid_agent = {p.puuid: p.agent for p in match.players}
    results: list[ExtractedRotation] = []

    for rd in match.rounds:
        # Gather all spatial waypoints per player
        player_waypoints: dict[str, list[RotationEvent]] = {}

        # 1. Kills
        for k in rd.kills:
            t = k.time_in_round_ms
            # Living players snapshot
            for pl in k.player_locations:
                if pl.location and pl.location.is_plausible:
                    player_waypoints.setdefault(pl.puuid, []).append(
                        RotationEvent(t, pl.location.x, pl.location.y)
                    )
            # Killer location
            if k.killer_location and k.killer_location.is_plausible:
                player_waypoints.setdefault(k.killer_puuid, []).append(
                    RotationEvent(t, k.killer_location.x, k.killer_location.y)
                )
            # Victim location (final position)
            if k.victim_location and k.victim_location.is_plausible:
                player_waypoints.setdefault(k.victim_puuid, []).append(
                    RotationEvent(t, k.victim_location.x, k.victim_location.y)
                )

        # 2. Plant
        if rd.plant is not None:
            pt = rd.plant.round_time_ms
            if rd.plant.location and rd.plant.location.is_plausible:
                player_waypoints.setdefault(rd.plant.planter_puuid, []).append(
                    RotationEvent(pt, rd.plant.location.x, rd.plant.location.y)
                )
            for pl in rd.plant.player_locations:
                if pl.location and pl.location.is_plausible:
                    player_waypoints.setdefault(pl.puuid, []).append(
                        RotationEvent(pt, pl.location.x, pl.location.y)
                    )

        # 3. Defuse
        if rd.defuse is not None:
            dt = rd.defuse.round_time_ms
            if rd.defuse.location and rd.defuse.location.is_plausible:
                player_waypoints.setdefault(rd.defuse.defuser_puuid, []).append(
                    RotationEvent(dt, rd.defuse.location.x, rd.defuse.location.y)
                )

        # Extract transitions
        for puuid, waypoints in player_waypoints.items():
            if len(waypoints) < 2:
                continue

            # Sort chronologically
            waypoints.sort(key=lambda w: w.t_ms)

            team = puuid_team.get(puuid, "")
            side = rd.team_sides.get(team, Side.NONE)
            side_int = 1 if side == Side.ATTACK else (2 if side == Side.DEFENSE else 0)
            won_int = 1 if rd.winning_team and rd.winning_team == team else 0

            last_zone: str | None = None
            last_t: int | None = None

            for wp in waypoints:
                z = resolve_zone_from_callouts(map_name, wp.x, wp.y, callout_tuples)
                if z == "Unknown":
                    continue

                if last_zone is not None and z != last_zone and last_t is not None:
                    # Ignore zero-duration transitions or backwards time
                    if wp.t_ms > last_t:
                        results.append(
                            ExtractedRotation(
                                round_num=rd.number,
                                side=side_int,
                                player_puuid=puuid,
                                from_zone=last_zone,
                                to_zone=z,
                                t_start_ms=last_t,
                                t_end_ms=wp.t_ms,
                                won=won_int,
                                agent=puuid_agent.get(puuid, ""),
                                team=team,
                            )
                        )
                last_zone = z
                last_t = wp.t_ms

    return results
