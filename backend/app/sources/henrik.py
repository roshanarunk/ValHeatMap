"""Adapter for the HenrikDev (unofficial) API, match endpoint v4.

Shape differs from Riot's in three important ways:
  * kills live in one flat `data.kills` array, each tagged with `round`
  * plants/defuses hang off each round as `round.plant` / `round.defuse`
  * players, agents, weapons and maps arrive pre-resolved as {id, name}

Docs: https://docs.henrikdev.xyz/valorant/api-reference/match
"""

from __future__ import annotations

from typing import Any

from ..models import (
    AbilitySlot,
    DamageType,
    Defuse,
    Kill,
    Match,
    MatchMeta,
    Plant,
    Player,
    PlayerLocation,
    Point,
    Round,
    Side,
)
from ..reference import SLOT_ALIASES, get_agent, get_map, get_weapon

MODE_MAP = {
    "competitive": "standard",
    "unrated": "standard",
    "standard": "standard",
    "deathmatch": "deathmatch",
    "team deathmatch": "team_deathmatch",
    "teamdeathmatch": "team_deathmatch",
    "hurm": "team_deathmatch",
    "escalation": "escalation",
    "swiftplay": "swiftplay",
    "spike rush": "spikerush",
    "spikerush": "spikerush",
    "replication": "replication",
    "premier": "standard",
}

HALF = 12


def _point(raw: Any) -> Point | None:
    if not isinstance(raw, dict):
        return None
    x, y = raw.get("x"), raw.get("y")
    if x is None or y is None:
        return None
    point = Point(float(x), float(y))
    # Drop Riot's out-of-world sentinel rather than plotting it.
    return point if point.is_plausible else None


def _player_ref(raw: Any) -> tuple[str, str]:
    """(puuid, team) from a HenrikDev player reference object."""
    if not isinstance(raw, dict):
        return "", ""
    return raw.get("puuid") or "", raw.get("team") or ""


def _locations(raw: Any) -> list[PlayerLocation]:
    out: list[PlayerLocation] = []
    for pl in raw or ():
        if not isinstance(pl, dict):
            continue
        point = _point(pl.get("location"))
        if point is None:
            continue
        puuid, _ = _player_ref(pl.get("player"))
        out.append(
            PlayerLocation(
                puuid=puuid,
                location=point,
                view_radians=float(pl.get("view_radians") or 0.0),
            )
        )
    return out


def _side_for(round_num: int, team: str, attacker_team: str) -> Side:
    if round_num < HALF:
        swapped = False
    elif round_num < HALF * 2:
        swapped = True
    else:
        swapped = (round_num - HALF * 2) % 2 == 0
    current = ("red" if attacker_team == "blue" else "blue") if swapped else attacker_team
    return Side.ATTACK if team.lower() == current.lower() else Side.DEFENSE


def _normalise_team(team: str) -> str:
    """HenrikDev uses lowercase 'red'/'blue'; match Riot's capitalisation."""
    t = (team or "").lower()
    if t in {"red", "blue"}:
        return t.capitalize()
    return team or ""


def parse(data: dict[str, Any], source: str = "henrik") -> Match:
    # Accept either the full envelope {status, data:{...}} or the inner object.
    if "data" in data and isinstance(data["data"], dict) and "metadata" in data["data"]:
        data = data["data"]

    meta_raw = data.get("metadata") or {}
    map_raw = meta_raw.get("map") or {}
    map_name = map_raw.get("name") if isinstance(map_raw, dict) else str(map_raw or "")
    map_info = get_map(map_name or "")
    queue_raw = meta_raw.get("queue") or {}
    queue = queue_raw.get("name") if isinstance(queue_raw, dict) else str(queue_raw or "")
    mode_key = (queue_raw.get("mode_type") if isinstance(queue_raw, dict) else "") or queue
    mode = MODE_MAP.get(str(mode_key).lower(), str(mode_key).lower() or "standard")

    started = meta_raw.get("started_at")
    started_ms = 0
    if isinstance(started, (int, float)):
        started_ms = int(started if started > 10**12 else started * 1000)
    elif isinstance(started, str) and started:
        from datetime import datetime

        try:
            started_ms = int(
                datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            started_ms = 0

    meta = MatchMeta(
        match_id=meta_raw.get("match_id") or "",
        map_id=map_info.map_url if map_info else (map_name or ""),
        map_name=map_info.name if map_info else (map_name or ""),
        mode=mode,
        mode_raw=str(mode_key or ""),
        queue=str(queue or ""),
        started_at=started_ms,
        game_length_ms=int(meta_raw.get("game_length_in_ms") or 0),
        game_version=meta_raw.get("game_version") or "",
        region=meta_raw.get("region") or "",
        source=source,
        is_ranked=str(queue_raw.get("id") if isinstance(queue_raw, dict) else "").lower()
        == "competitive",
    )

    players: list[Player] = []
    team_by_puuid: dict[str, str] = {}
    agent_by_puuid: dict[str, Any] = {}
    for p in data.get("players") or ():
        puuid = p.get("puuid") or ""
        agent_raw = p.get("agent") or {}
        agent = get_agent(agent_raw.get("id") or agent_raw.get("name") or "")
        agent_by_puuid[puuid] = agent
        team = _normalise_team(p.get("team_id") or "")
        team_by_puuid[puuid] = team
        stats = p.get("stats") or {}
        players.append(
            Player(
                puuid=puuid,
                name=p.get("name") or "",
                tag=p.get("tag") or "",
                team=team,
                agent=agent.name if agent else (agent_raw.get("name") or ""),
                agent_id=agent.uuid if agent else (agent_raw.get("id") or ""),
                kills=int(stats.get("kills") or 0),
                deaths=int(stats.get("deaths") or 0),
                assists=int(stats.get("assists") or 0),
                score=int(stats.get("score") or 0),
                damage=int(((stats.get("damage") or {}).get("dealt")) or 0),
                headshots=int(stats.get("headshots") or 0),
                bodyshots=int(stats.get("bodyshots") or 0),
                legshots=int(stats.get("legshots") or 0),
                tier=int((p.get("tier") or {}).get("id") or 0),
            )
        )

    teams: dict[str, bool] = {}
    for t in data.get("teams") or ():
        tid = _normalise_team(t.get("team_id") or "")
        if tid:
            teams[tid] = bool(t.get("won"))

    rounds_raw = list(data.get("rounds") or ())
    sided = mode in {"standard", "swiftplay", "spikerush", "replication"}

    # Infer the first-half attacker from plants, exactly as for Riot data.
    attacker_team = ""
    if sided:
        votes: dict[str, int] = {}
        for idx, rnd in enumerate(rounds_raw):
            plant = rnd.get("plant")
            if not isinstance(plant, dict):
                continue
            _, pteam = _player_ref(plant.get("player"))
            pteam = _normalise_team(pteam)
            if not pteam:
                continue
            num = int(rnd.get("id", idx))
            key = pteam if num < HALF else ("Red" if pteam == "Blue" else "Blue")
            votes[key] = votes.get(key, 0) + 1
        attacker_team = max(votes, key=lambda k: votes[k]) if votes else "Red"

    # Kills are flat; bucket them by round.
    kills_by_round: dict[int, list[Kill]] = {}
    for k in data.get("kills") or ():
        num = int(k.get("round") or 0)
        killer_puuid, killer_team = _player_ref(k.get("killer"))
        victim_puuid, victim_team = _player_ref(k.get("victim"))
        killer_team = _normalise_team(killer_team) or team_by_puuid.get(killer_puuid, "")
        victim_team = _normalise_team(victim_team) or team_by_puuid.get(victim_puuid, "")

        locs = _locations(k.get("player_locations"))
        killer_loc = next((pl.location for pl in locs if pl.puuid == killer_puuid), None)

        weapon_raw = k.get("weapon") or {}
        wtype = str(weapon_raw.get("type") or "").lower()
        wid = weapon_raw.get("id") or ""
        wname = weapon_raw.get("name") or ""
        slot: AbilitySlot | None = None
        ability_name = ""
        if wtype == "ability":
            dtype = DamageType.ABILITY
            key = SLOT_ALIASES.get(str(wname or wid).lower())
            if key:
                try:
                    slot = AbilitySlot(key)
                except ValueError:
                    slot = None
            agent = agent_by_puuid.get(killer_puuid)
            if slot is not None and agent is not None:
                ability_name = agent.ability_name(slot.value)
            if not ability_name and wname and not key:
                # HenrikDev sometimes resolves the ability name directly.
                ability_name = wname
        elif wtype == "bomb":
            dtype = DamageType.BOMB
        elif wtype in {"weapon", "melee"}:
            dtype = DamageType.WEAPON
        else:
            dtype = DamageType.UNKNOWN

        weapon = get_weapon(wid or wname) if dtype is DamageType.WEAPON else None
        side = (
            _side_for(num, killer_team, attacker_team)
            if sided and attacker_team and killer_team
            else Side.NONE
        )

        kills_by_round.setdefault(num, []).append(
            Kill(
                round_num=num,
                time_in_round_ms=int(k.get("time_in_round_in_ms") or 0),
                time_in_match_ms=int(k.get("time_in_match_in_ms") or 0),
                killer_puuid=killer_puuid,
                victim_puuid=victim_puuid,
                victim_location=_point(k.get("location")),
                killer_location=killer_loc,
                assistants=[_player_ref(a)[0] for a in (k.get("assistants") or ())],
                player_locations=locs,
                damage_type=dtype,
                weapon_id=wid if dtype is DamageType.WEAPON else "",
                weapon_name=(weapon.name if weapon else wname) if dtype is DamageType.WEAPON else "",
                ability_slot=slot,
                ability_name=ability_name,
                secondary_fire=bool(k.get("secondary_fire_mode")),
                killer_side=side,
                killer_team=killer_team,
                victim_team=victim_team,
            )
        )

    rounds: list[Round] = []
    for idx, rnd in enumerate(rounds_raw):
        num = int(rnd.get("id", idx))
        winning_team = _normalise_team(rnd.get("winning_team") or "")
        team_sides: dict[str, Side] = {}
        if sided and attacker_team:
            for tid in teams or {"Red": False, "Blue": False}:
                team_sides[tid] = _side_for(num, tid, attacker_team)

        plant: Plant | None = None
        plant_raw = rnd.get("plant")
        if isinstance(plant_raw, dict):
            loc = _point(plant_raw.get("location"))
            if loc is not None:
                planter, pteam = _player_ref(plant_raw.get("player"))
                pteam = _normalise_team(pteam) or team_by_puuid.get(planter, "")
                plant = Plant(
                    round_num=num,
                    round_time_ms=int(plant_raw.get("round_time_in_ms") or 0),
                    site=plant_raw.get("site") or "",
                    location=loc,
                    planter_puuid=planter,
                    planter_team=pteam,
                    player_locations=_locations(plant_raw.get("player_locations")),
                    won=bool(pteam) and winning_team == pteam,
                    defused=isinstance(rnd.get("defuse"), dict),
                )

        defuse: Defuse | None = None
        defuse_raw = rnd.get("defuse")
        if isinstance(defuse_raw, dict):
            loc = _point(defuse_raw.get("location"))
            if loc is not None:
                defuser, dteam = _player_ref(defuse_raw.get("player"))
                defuse = Defuse(
                    round_num=num,
                    round_time_ms=int(defuse_raw.get("round_time_in_ms") or 0),
                    location=loc,
                    defuser_puuid=defuser,
                    defuser_team=_normalise_team(dteam) or team_by_puuid.get(defuser, ""),
                )

        kills = sorted(
            kills_by_round.pop(num, []), key=lambda k: (k.time_in_round_ms, k.time_in_match_ms)
        )
        rounds.append(
            Round(
                number=num,
                winning_team=winning_team,
                result=str(rnd.get("result") or ""),
                ceremony=str(rnd.get("ceremony") or ""),
                plant=plant,
                defuse=defuse,
                kills=kills,
                team_sides=team_sides,
            )
        )

    # Modes without a rounds array (deathmatch) still have kills; keep them.
    for num, kills in sorted(kills_by_round.items()):
        rounds.append(
            Round(
                number=num,
                winning_team="",
                result="",
                kills=sorted(kills, key=lambda k: (k.time_in_round_ms, k.time_in_match_ms)),
            )
        )

    return Match(meta=meta, players=players, rounds=rounds, teams=teams)


def looks_like(data: dict[str, Any]) -> bool:
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    return isinstance(inner, dict) and "metadata" in inner and "players" in inner
