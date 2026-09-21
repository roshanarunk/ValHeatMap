"""Adapter for the official Riot match-v1 payload.

Handles both field spellings Riot has shipped over the years:
  * current:  `subject`, `roundTime`, `gameStartMillis`, `queueID`
  * older:    `puuid`,   `timeSinceRoundStartMillis`, `queueId`
Both appear in the bundled sample matches, so both are supported.
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

# Riot game-mode asset paths -> our normalised mode ids.
MODE_MAP = {
    "bomb": "standard",
    "deathmatch": "deathmatch",
    "teamdeathmatch": "team_deathmatch",
    "ggteam": "escalation",
    "swiftplay": "swiftplay",
    "spikerush": "spikerush",
    "onefa": "replication",
    "hurm": "team_deathmatch",
    "quickbomb": "spikerush",
}

# Standard competitive: first 12 rounds one side, then a swap. Overtime
# alternates each round. Round numbers are 0-indexed in the payload.
REGULATION_ROUNDS = 12
HALF = 12


def _sub(obj: dict[str, Any]) -> str:
    """Player identifier, whichever spelling this payload uses."""
    return obj.get("subject") or obj.get("puuid") or ""


def _round_time(kill: dict[str, Any]) -> int:
    v = kill.get("roundTime")
    if v is None:
        v = kill.get("timeSinceRoundStartMillis")
    return int(v or 0)


def _match_time(kill: dict[str, Any]) -> int:
    v = kill.get("gameTime")
    if v is None:
        v = kill.get("timeSinceGameStartMillis")
    return int(v or 0)


def _point(raw: Any) -> Point | None:
    if not isinstance(raw, dict):
        return None
    x, y = raw.get("x"), raw.get("y")
    if x is None or y is None:
        return None
    point = Point(float(x), float(y))
    # Drop Riot's out-of-world sentinel rather than plotting it.
    return point if point.is_plausible else None


def _normalise_mode(game_mode: str) -> tuple[str, str]:
    """(normalised id, raw label)"""
    raw = (game_mode or "").rsplit("/", 1)[-1]
    stem = raw.split(".")[0].replace("GameMode", "").replace("_C", "").lower()
    return MODE_MAP.get(stem, stem or "standard"), raw


def _side_for(round_num: int, team: str, attacker_team: str) -> Side:
    """Which side `team` played in `round_num` (0-indexed).

    Rounds 0-11 keep the starting sides, 12-23 swap them, and overtime
    swaps again every round.
    """
    if round_num < HALF:
        swapped = False
    elif round_num < HALF * 2:
        swapped = True
    else:
        swapped = (round_num - HALF * 2) % 2 == 0
    current_attacker = ("Red" if attacker_team == "Blue" else "Blue") if swapped else attacker_team
    return Side.ATTACK if team == current_attacker else Side.DEFENSE


def _infer_attacker_team(data: dict[str, Any], team_by_puuid: dict[str, str]) -> str:
    """Riot never states which team started on attack; infer it from plants.

    Only attackers can plant the spike, so every plant is direct evidence of
    who was attacking that round. Each plant votes for the team that must
    have attacked in the *first* half (flipping the vote for second-half
    rounds). Falls back to "Red", Riot's usual first-attacker convention.
    """
    votes: dict[str, int] = {}
    for rnd in data.get("roundResults") or ():
        if not (rnd.get("plantSite") or ""):
            continue
        planter_team = team_by_puuid.get(rnd.get("bombPlanter") or "", "")
        if not planter_team:
            # Fall back to the winner of a detonation round: only the
            # attacking side can win by the spike exploding.
            if rnd.get("roundResultCode") == "Detonate":
                planter_team = rnd.get("winningTeam") or ""
        if not planter_team:
            continue
        num = int(rnd.get("roundNum", 0))
        key = planter_team if num < HALF else ("Red" if planter_team == "Blue" else "Blue")
        votes[key] = votes.get(key, 0) + 1
    if votes:
        return max(votes, key=lambda k: votes[k])
    return "Red"


def _ability_slot(damage_item: str) -> AbilitySlot | None:
    key = SLOT_ALIASES.get((damage_item or "").lower())
    if not key:
        return None
    try:
        return AbilitySlot(key)
    except ValueError:
        return None


def parse(data: dict[str, Any], source: str = "riot") -> Match:
    info = data.get("matchInfo") or {}
    map_id = info.get("mapId") or ""
    map_info = get_map(map_id)
    mode, mode_raw = _normalise_mode(info.get("gameMode") or "")

    meta = MatchMeta(
        match_id=info.get("matchId") or "",
        map_id=map_id,
        map_name=map_info.name if map_info else map_id.rsplit("/", 1)[-1],
        mode=mode,
        mode_raw=mode_raw,
        queue=info.get("queueID") or info.get("queueId") or "",
        started_at=int(info.get("gameStartMillis") or 0),
        game_length_ms=int(info.get("gameLengthMillis") or 0),
        game_version=info.get("gameVersion") or "",
        source=source,
        is_ranked=bool(info.get("isRanked")),
    )

    players: list[Player] = []
    agent_by_puuid: dict[str, Any] = {}
    team_by_puuid: dict[str, str] = {}
    for p in data.get("players") or ():
        puuid = _sub(p)
        stats = p.get("stats") or {}
        agent = get_agent(p.get("characterId") or "")
        agent_by_puuid[puuid] = agent
        team = p.get("teamId") or ""
        team_by_puuid[puuid] = team
        players.append(
            Player(
                puuid=puuid,
                name=p.get("gameName") or "",
                tag=p.get("tagLine") or "",
                team=team,
                agent=agent.name if agent else (p.get("characterId") or ""),
                agent_id=agent.uuid if agent else (p.get("characterId") or ""),
                kills=int(stats.get("kills") or 0),
                deaths=int(stats.get("deaths") or 0),
                assists=int(stats.get("assists") or 0),
                score=int(stats.get("score") or 0),
                rounds_played=int(stats.get("roundsPlayed") or 0),
                tier=int(p.get("competitiveTier") or 0),
            )
        )

    # Damage/shot totals: prefer the per-round `damage` entries inside
    # playerStats (they carry shot breakdowns); fall back to the flat
    # `roundDamage` list on the player object when playerStats lacks them.
    damage_totals: dict[str, int] = {}
    shots: dict[str, list[int]] = {}
    for rnd in data.get("roundResults") or ():
        for ps in rnd.get("playerStats") or ():
            puuid = _sub(ps)
            agg = shots.setdefault(puuid, [0, 0, 0])
            for dmg in ps.get("damage") or ():
                damage_totals[puuid] = damage_totals.get(puuid, 0) + int(dmg.get("damage") or 0)
                agg[0] += int(dmg.get("headshots") or 0)
                agg[1] += int(dmg.get("bodyshots") or 0)
                agg[2] += int(dmg.get("legshots") or 0)
    for p in data.get("players") or ():
        puuid = _sub(p)
        if damage_totals.get(puuid):
            continue
        total = sum(int(d.get("damage") or 0) for d in (p.get("roundDamage") or ()))
        if total:
            damage_totals[puuid] = total

    teams: dict[str, bool] = {}
    for t in data.get("teams") or ():
        tid = t.get("teamId") or ""
        if tid:
            teams[tid] = bool(t.get("won"))

    sided = mode in {"standard", "swiftplay", "spikerush", "replication"}
    attacker_team = _infer_attacker_team(data, team_by_puuid) if sided else ""

    rounds: list[Round] = []
    for rnd in data.get("roundResults") or ():
        num = int(rnd.get("roundNum", 0))
        winning_team = rnd.get("winningTeam") or ""
        team_sides: dict[str, Side] = {}
        if sided and attacker_team:
            for tid in teams or {"Red": False, "Blue": False}:
                team_sides[tid] = _side_for(num, tid, attacker_team)

        # --- kills -------------------------------------------------------
        kills: list[Kill] = []
        for ps in rnd.get("playerStats") or ():
            killer_puuid = _sub(ps)
            agent = agent_by_puuid.get(killer_puuid)
            for k in ps.get("kills") or ():
                victim = k.get("victim") or ""
                # Riot omits the killer's own position from most payloads;
                # recover it from the location snapshot taken at kill time.
                locs = [
                    PlayerLocation(
                        puuid=_sub(pl),
                        location=_point(pl.get("location")) or Point(0.0, 0.0),
                        view_radians=float(pl.get("viewRadians") or 0.0),
                    )
                    for pl in (k.get("playerLocations") or ())
                    if _point(pl.get("location"))
                ]
                killer_loc = next(
                    (pl.location for pl in locs if pl.puuid == killer_puuid), None
                )

                fd = k.get("finishingDamage") or {}
                raw_type = (fd.get("damageType") or "").lower()
                damage_item = fd.get("damageItem") or ""
                slot = _ability_slot(damage_item) if raw_type == "ability" else None
                try:
                    dtype = DamageType(raw_type) if raw_type else DamageType.UNKNOWN
                except ValueError:
                    dtype = DamageType.UNKNOWN

                weapon = get_weapon(damage_item) if dtype is DamageType.WEAPON else None
                ability_name = ""
                if slot is not None and agent is not None:
                    ability_name = agent.ability_name(slot.value)

                kt = team_by_puuid.get(killer_puuid, "")
                vt = team_by_puuid.get(victim, "")
                kills.append(
                    Kill(
                        round_num=num,
                        time_in_round_ms=_round_time(k),
                        time_in_match_ms=_match_time(k),
                        killer_puuid=killer_puuid,
                        victim_puuid=victim,
                        victim_location=_point(k.get("victimLocation")),
                        killer_location=killer_loc,
                        assistants=[a for a in (k.get("assistants") or ()) if isinstance(a, str)],
                        player_locations=locs,
                        damage_type=dtype,
                        weapon_id=damage_item if dtype is DamageType.WEAPON else "",
                        weapon_name=weapon.name if weapon else "",
                        ability_slot=slot,
                        ability_name=ability_name,
                        secondary_fire=bool(fd.get("isSecondaryFireMode")),
                        killer_side=team_sides.get(kt, Side.NONE),
                        killer_team=kt,
                        victim_team=vt,
                    )
                )
        kills.sort(key=lambda k: (k.time_in_round_ms, k.time_in_match_ms))

        # --- plant / defuse ---------------------------------------------
        plant: Plant | None = None
        site = rnd.get("plantSite") or ""
        plant_loc = _point(rnd.get("plantLocation"))
        # Riot writes {0,0} for "no plant"; treat that as absent.
        if site and plant_loc and not (plant_loc.x == 0 and plant_loc.y == 0):
            plocs = [
                PlayerLocation(
                    puuid=_sub(pl),
                    location=_point(pl.get("location")) or Point(0.0, 0.0),
                    view_radians=float(pl.get("viewRadians") or 0.0),
                )
                for pl in (rnd.get("plantPlayerLocations") or ())
                if _point(pl.get("location"))
            ]
            planter = rnd.get("bombPlanter") or ""
            planter_team = team_by_puuid.get(planter, "")
            if not planter_team and sided and attacker_team:
                planter_team = next(
                    (t for t, s in team_sides.items() if s is Side.ATTACK), ""
                )
            plant = Plant(
                round_num=num,
                round_time_ms=int(rnd.get("plantRoundTime") or 0),
                site=site,
                location=plant_loc,
                planter_puuid=planter,
                planter_team=planter_team,
                player_locations=plocs,
                won=bool(planter_team) and winning_team == planter_team,
                defused=rnd.get("roundResultCode") == "Defuse",
            )

        defuse: Defuse | None = None
        defuse_loc = _point(rnd.get("defuseLocation"))
        if defuse_loc and not (defuse_loc.x == 0 and defuse_loc.y == 0):
            defuser = rnd.get("bombDefuser") or ""
            defuse = Defuse(
                round_num=num,
                round_time_ms=int(rnd.get("defuseRoundTime") or 0),
                location=defuse_loc,
                defuser_puuid=defuser,
                defuser_team=team_by_puuid.get(defuser, ""),
            )

        rounds.append(
            Round(
                number=num,
                winning_team=winning_team,
                result=rnd.get("roundResultCode") or rnd.get("roundResult") or "",
                ceremony=rnd.get("roundCeremony") or "",
                plant=plant,
                defuse=defuse,
                kills=kills,
                team_sides=team_sides,
            )
        )

    for player in players:
        player.damage = damage_totals.get(player.puuid, 0)
        hs, bs, ls = shots.get(player.puuid, (0, 0, 0))
        player.headshots, player.bodyshots, player.legshots = hs, bs, ls

    return Match(meta=meta, players=players, rounds=rounds, teams=teams)


def looks_like(data: dict[str, Any]) -> bool:
    return "matchInfo" in data and "roundResults" in data
