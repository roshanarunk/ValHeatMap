"""Kill-level analytics: filtering, trade detection, heatmap binning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..models import DamageType, Kill, Match, Side
from ..reference import MapInfo

# --- trade detection defaults ------------------------------------------
# A death is "traded" when a teammate kills the original killer soon after.
# 3s is the window analysts conventionally use. The radius keeps a trade
# tied to the fight it avenges: measured over the bundled 5v5 matches,
# genuine revenge kills land a median 1358 units from the original death
# and 90% fall within ~2850, so 3000 (~30m) keeps almost all real trades
# while still rejecting unrelated kills elsewhere on the map.
TRADE_WINDOW_MS = 3000
TRADE_RADIUS = 3000.0


@dataclass(slots=True)
class KillFilters:
    agents: frozenset[str] = frozenset()        # agent display names (killer)
    victim_agents: frozenset[str] = frozenset()
    players: frozenset[str] = frozenset()       # puuids (killer)
    victim_players: frozenset[str] = frozenset()
    teams: frozenset[str] = frozenset()
    sides: frozenset[str] = frozenset()         # "attack" / "defense"
    rounds: frozenset[int] = frozenset()
    weapons: frozenset[str] = frozenset()       # weapon display names
    victim_weapons: frozenset[str] = frozenset() # victim weapon display names
    abilities: frozenset[str] = frozenset()     # ability display names
    damage_types: frozenset[str] = frozenset()
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    utility_only: bool = False
    traded_only: bool = False
    untraded_only: bool = False
    first_blood_only: bool = False
    post_plant_only: bool = False
    pre_plant_only: bool = False
    exclude_teamkills: bool = True

    @classmethod
    def from_query(cls, params: dict[str, Any]) -> "KillFilters":
        def sset(key: str) -> frozenset[str]:
            raw = params.get(key)
            if not raw:
                return frozenset()
            if isinstance(raw, str):
                raw = [v for v in raw.split(",") if v]
            return frozenset(str(v) for v in raw)

        def iset(key: str) -> frozenset[int]:
            raw = params.get(key)
            if not raw:
                return frozenset()
            if isinstance(raw, str):
                raw = [v for v in raw.split(",") if v]
            out: set[int] = set()
            for v in raw:
                try:
                    out.add(int(v))
                except (TypeError, ValueError):
                    continue
            return frozenset(out)

        def flag(key: str) -> bool:
            v = params.get(key)
            return str(v).lower() in {"1", "true", "yes", "on"} if v is not None else False

        def num(key: str) -> int | None:
            v = params.get(key)
            if v in (None, ""):
                return None
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        return cls(
            agents=sset("agents"),
            victim_agents=sset("victim_agents"),
            players=sset("players"),
            victim_players=sset("victim_players"),
            teams=sset("teams"),
            sides=sset("sides"),
            rounds=iset("rounds"),
            weapons=sset("weapons"),
            victim_weapons=sset("victim_weapons"),
            abilities=sset("abilities"),
            damage_types=sset("damage_types"),
            time_start_ms=num("time_start"),
            time_end_ms=num("time_end"),
            utility_only=flag("utility_only"),
            traded_only=flag("traded_only"),
            untraded_only=flag("untraded_only"),
            first_blood_only=flag("first_blood_only"),
            post_plant_only=flag("post_plant_only"),
            pre_plant_only=flag("pre_plant_only"),
            exclude_teamkills=not flag("include_teamkills"),
        )


@dataclass(slots=True)
class EnrichedKill:
    """A kill plus the derived context the UI needs."""
    kill: Kill
    killer_agent: str
    victim_agent: str
    killer_name: str
    victim_name: str
    traded: bool               # the victim's death was avenged
    trade_kill: bool           # this kill avenged a teammate
    trade_latency_ms: int | None
    first_blood: bool
    post_plant: bool
    round_won: bool            # did the killer's team win this round
    supported: bool = False    # victim had a teammate within <= 12m
    isolated: bool = False     # victim's nearest teammate was > 25m or was alone
    crossfire: bool = False    # killer held angle with a teammate (35-145 deg) on victim
    advantage_death: bool = False  # victim died while team had >= 2 player advantage
    clutch_kill: bool = False  # killer was last alive (1vX) when getting kill
    low_impact: bool = False   # kill occurred in lost round at >= 2 deficit or post-detonate


def _distance(a, b) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def enrich(
    match: Match,
    trade_window_ms: int = TRADE_WINDOW_MS,
    trade_radius: float = TRADE_RADIUS,
) -> list[EnrichedKill]:
    """Annotate every kill with trade/first-blood/post-plant context.

    Trade rule: victim V is killed by K at time t. V's death counts as traded
    if a teammate of V kills K within `trade_window_ms`, and that revenge kill
    happens near where V died (within `trade_radius` world units) -- the
    proximity check is what separates a genuine trade from an unrelated kill
    elsewhere on the map.
    """
    # Deathmatch has no real rounds, so "first kill of the round" carries no
    # meaning there and would otherwise mark almost every kill as an opening.
    track_openings = match.is_round_based
    out: list[EnrichedKill] = []

    # Map puuid -> player for fast lookup
    player_by_puuid = {p.puuid: p for p in match.players}
    team_counts: dict[str, int] = {}
    for p in match.players:
        if p.team:
            team_counts[p.team] = team_counts.get(p.team, 0) + 1

    for rnd in match.rounds:
        kills = rnd.kills
        plant_ms = rnd.plant.round_time_ms if rnd.plant else None
        alive_counts = dict(team_counts)

        for idx, kill in enumerate(kills):
            killer = player_by_puuid.get(kill.killer_puuid)
            victim = player_by_puuid.get(kill.victim_puuid)
            k_team = (killer.team if killer else "") or kill.killer_team
            v_team = (victim.team if victim else "") or kill.victim_team

            # Pre-kill alive counts
            k_alive = alive_counts.get(k_team, 0)
            v_alive = alive_counts.get(v_team, 0)

            # Did a teammate of the victim kill this killer shortly after?
            traded = False
            for later in kills[idx + 1 :]:
                dt = later.time_in_round_ms - kill.time_in_round_ms
                if dt > trade_window_ms:
                    break
                if later.victim_puuid != kill.killer_puuid:
                    continue
                if kill.victim_team and later.killer_team != kill.victim_team:
                    continue
                if trade_radius > 0 and kill.victim_location is not None:
                    ref = later.victim_location or kill.killer_location
                    if ref is not None and _distance(ref, kill.victim_location) > trade_radius:
                        continue
                traded = True
                break

            # Did this kill avenge a teammate who just died?
            trade_kill = False
            latency: int | None = None
            for earlier in reversed(kills[:idx]):
                dt = kill.time_in_round_ms - earlier.time_in_round_ms
                if dt > trade_window_ms:
                    break
                if earlier.killer_puuid != kill.victim_puuid:
                    continue
                if kill.killer_team and earlier.victim_team != kill.killer_team:
                    continue
                if trade_radius > 0:
                    ref = kill.victim_location
                    if (
                        ref is not None
                        and earlier.victim_location is not None
                        and _distance(ref, earlier.victim_location) > trade_radius
                    ):
                        continue
                    trade_kill = True
                    latency = dt
                    break

            # --- Support & Isolation (Victim spacing) ---
            supported = False
            isolated = False
            if kill.victim_location is not None:
                teammate_dists = [
                    _distance(pl.location, kill.victim_location)
                    for pl in kill.player_locations
                    if pl.puuid != kill.victim_puuid
                    and player_by_puuid.get(pl.puuid) is not None
                    and player_by_puuid[pl.puuid].team == v_team
                ]
                if teammate_dists:
                    min_dist = min(teammate_dists)
                    supported = min_dist <= 1200.0  # 12m trade support
                    isolated = min_dist > 2500.0   # 25m isolated
                else:
                    isolated = True  # last alive on team

            # --- Crossfire Kill ---
            crossfire = False
            if kill.killer_location is not None and kill.victim_location is not None:
                dx_k = kill.victim_location.x - kill.killer_location.x
                dy_k = kill.victim_location.y - kill.killer_location.y
                dist_k = math.hypot(dx_k, dy_k)
                if dist_k > 100.0:
                    for pl in kill.player_locations:
                        if pl.puuid != kill.killer_puuid:
                            tpl = player_by_puuid.get(pl.puuid)
                            if tpl and tpl.team == k_team:
                                dx_t = kill.victim_location.x - pl.location.x
                                dy_t = kill.victim_location.y - pl.location.y
                                dist_t = math.hypot(dx_t, dy_t)
                                if 100.0 < dist_t <= 3500.0:  # teammate within 35m
                                    dot = dx_k * dx_t + dy_k * dy_t
                                    cos_a = max(-1.0, min(1.0, dot / (dist_k * dist_t)))
                                    deg = math.degrees(math.acos(cos_a))
                                    if 35.0 <= deg <= 145.0:
                                        crossfire = True
                                        break

            round_won = bool(kill.killer_team) and rnd.winning_team == kill.killer_team
            is_fb = track_openings and idx == 0

            # --- Man-Advantage Casualty (Over-peek) ---
            adv_death = bool(v_team and k_team and v_team != k_team and (v_alive - k_alive >= 2))

            # --- Clutch Kill (1vX) ---
            clutch_kill = bool(k_team and v_team and k_team != v_team and k_alive == 1 and v_alive >= 1)

            # --- Low Impact Kill ---
            # Kill in a lost round during a >= 2 deficit, excluding opening kills
            deficit = (v_alive - k_alive >= 2) if (v_team and k_team and v_team != k_team) else False
            low_impact = (not round_won) and (not is_fb) and deficit

            out.append(
                EnrichedKill(
                    kill=kill,
                    killer_agent=killer.agent if killer else "",
                    victim_agent=victim.agent if victim else "",
                    killer_name=killer.display if killer else "",
                    victim_name=victim.display if victim else "",
                    traded=traded,
                    trade_kill=trade_kill,
                    trade_latency_ms=latency,
                    first_blood=is_fb,
                    post_plant=plant_ms is not None and kill.time_in_round_ms >= plant_ms,
                    round_won=round_won,
                    supported=supported,
                    isolated=isolated,
                    crossfire=crossfire,
                    advantage_death=adv_death,
                    clutch_kill=clutch_kill,
                    low_impact=low_impact,
                )
            )

            # Update living counts
            if v_team in alive_counts:
                alive_counts[v_team] = max(0, alive_counts[v_team] - 1)
    return out


def apply_filters(kills: Iterable[EnrichedKill], f: KillFilters) -> list[EnrichedKill]:
    out: list[EnrichedKill] = []
    for ek in kills:
        k = ek.kill
        if f.exclude_teamkills and k.is_teamkill:
            continue
        if f.agents and ek.killer_agent not in f.agents:
            continue
        if f.victim_agents and ek.victim_agent not in f.victim_agents:
            continue
        if f.players and k.killer_puuid not in f.players:
            continue
        if f.victim_players and k.victim_puuid not in f.victim_players:
            continue
        if f.teams and k.killer_team not in f.teams:
            continue
        if f.sides and k.killer_side.value not in f.sides:
            continue
        if f.rounds and k.round_num not in f.rounds:
            continue
        if f.weapons and k.weapon_name not in f.weapons:
            continue
        if f.victim_weapons and k.victim_weapon_name not in f.victim_weapons:
            continue
        if f.abilities and k.ability_name not in f.abilities:
            continue
        if f.damage_types and k.damage_type.value not in f.damage_types:
            continue
        if f.utility_only and k.damage_type is not DamageType.ABILITY:
            continue
        if f.time_start_ms is not None and k.time_in_round_ms < f.time_start_ms:
            continue
        if f.time_end_ms is not None and k.time_in_round_ms > f.time_end_ms:
            continue
        if f.traded_only and not ek.traded:
            continue
        if f.untraded_only and ek.traded:
            continue
        if f.first_blood_only and not ek.first_blood:
            continue
        if f.post_plant_only and not ek.post_plant:
            continue
        if f.pre_plant_only and ek.post_plant:
            continue
        out.append(ek)
    return out


def to_points(
    kills: Sequence[EnrichedKill],
    map_info: MapInfo,
    anchor: str = "victim",
) -> list[dict[str, Any]]:
    """Project kills into normalised minimap space for the renderer.

    `anchor` picks which end of the duel to plot: where people died
    ("victim"), where the killer stood ("killer"), or both ends joined.
    """
    pts: list[dict[str, Any]] = []
    for ek in kills:
        k = ek.kill
        entry: dict[str, Any] = {
            "round": k.round_num,
            "t": k.time_in_round_ms,
            "killer": ek.killer_name,
            "victim": ek.victim_name,
            "killer_agent": ek.killer_agent,
            "victim_agent": ek.victim_agent,
            "killer_team": k.killer_team,
            "side": k.killer_side.value,
            "weapon": k.weapon_name,
            "ability": k.ability_name,
            "type": k.damage_type.value,
            "traded": ek.traded,
            "trade_kill": ek.trade_kill,
            "first_blood": ek.first_blood,
            "post_plant": ek.post_plant,
            "round_won": ek.round_won,
        }
        if k.victim_location is None:
            # No usable death position; the kill still counts in the
            # summaries but there is nothing to plot.
            continue
        vx, vy = map_info.to_minimap(k.victim_location.x, k.victim_location.y)
        entry["victim_pos"] = {"x": vx, "y": vy}
        if k.killer_location is not None:
            kx, ky = map_info.to_minimap(k.killer_location.x, k.killer_location.y)
            entry["killer_pos"] = {"x": kx, "y": ky}
        if anchor == "killer" and "killer_pos" not in entry:
            continue
        pts.append(entry)
    return pts


def summarise(kills: Sequence[EnrichedKill], match: Match) -> dict[str, Any]:
    """Headline numbers for the filtered selection."""
    total = len(kills)
    if total == 0:
        return {
            "total": 0, "traded": 0, "trade_rate": 0.0, "utility": 0,
            "utility_rate": 0.0, "first_bloods": 0, "post_plant": 0,
            "round_win_rate": 0.0, "avg_trade_latency_ms": None,
        }
    traded = sum(1 for k in kills if k.traded)
    utility = sum(1 for k in kills if k.kill.damage_type is DamageType.ABILITY)
    latencies = [k.trade_latency_ms for k in kills if k.trade_latency_ms is not None]
    return {
        "total": total,
        "traded": traded,
        "trade_rate": round(traded / total, 4),
        "utility": utility,
        "utility_rate": round(utility / total, 4),
        "first_bloods": sum(1 for k in kills if k.first_blood),
        "post_plant": sum(1 for k in kills if k.post_plant),
        "round_win_rate": round(sum(1 for k in kills if k.round_won) / total, 4),
        "avg_trade_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
    }


def time_histogram(kills: Sequence[EnrichedKill], bucket_ms: int = 5000) -> list[dict[str, int]]:
    """Kills per time bucket -- drives the scrubber's density strip."""
    buckets: dict[int, int] = {}
    for ek in kills:
        b = (ek.kill.time_in_round_ms // bucket_ms) * bucket_ms
        buckets[b] = buckets.get(b, 0) + 1
    return [{"t": t, "count": c} for t, c in sorted(buckets.items())]
