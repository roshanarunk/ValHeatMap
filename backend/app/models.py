"""Canonical match model.

Every data source (Riot official match-v1, HenrikDev v4, uploaded JSON) is
normalised into these structures, so the analytics layer never has to know
where a match came from.

Coordinates
-----------
`Point.x` / `Point.y` are raw Valorant world units, exactly as Riot reports
them. Conversion to minimap space happens in `maps.to_minimap()` -- keeping
world units here means re-calibration never invalidates stored data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    ATTACK = "attack"
    DEFENSE = "defense"
    NONE = "none"  # deathmatch and other non-sided modes


class DamageType(str, Enum):
    WEAPON = "weapon"
    ABILITY = "ability"
    BOMB = "bomb"
    FALL = "fall"
    UNKNOWN = "unknown"


# Riot reports ability kills by loadout slot, not by name. The slot label
# differs between the official API and HenrikDev, so normalise to these four.
class AbilitySlot(str, Enum):
    ABILITY1 = "ability1"
    ABILITY2 = "ability2"
    GRENADE = "grenade"
    ULTIMATE = "ultimate"
    PASSIVE = "passive"


# Riot writes far-out-of-world coordinates (observed around -49800) when a
# position is unknown -- a player who fell out of the map, or died in a state
# the server did not track. Real map extents are well inside +/-20000, so
# anything beyond this is a sentinel rather than a location.
WORLD_LIMIT = 30_000.0


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    @property
    def is_plausible(self) -> bool:
        return abs(self.x) <= WORLD_LIMIT and abs(self.y) <= WORLD_LIMIT

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(slots=True)
class Player:
    puuid: str
    name: str
    tag: str
    team: str
    agent: str          # resolved display name, e.g. "Jett"
    agent_id: str       # uuid, for icon lookups
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    score: int = 0
    damage: int = 0
    headshots: int = 0
    bodyshots: int = 0
    legshots: int = 0
    rounds_played: int = 0
    tier: int = 0

    @property
    def display(self) -> str:
        return f"{self.name}#{self.tag}" if self.tag else self.name


@dataclass(slots=True)
class PlayerLocation:
    """Where a player stood at the instant of an event."""
    puuid: str
    location: Point
    view_radians: float = 0.0


@dataclass(slots=True)
class Kill:
    round_num: int
    time_in_round_ms: int
    time_in_match_ms: int
    killer_puuid: str
    victim_puuid: str
    # None when Riot reported an out-of-world sentinel for the death.
    victim_location: Point | None
    # Riot does not give the killer's position directly; it is recovered from
    # the `player_locations` snapshot taken at the moment of the kill. It can
    # legitimately be absent (e.g. the killer died in the same tick).
    killer_location: Point | None
    assistants: list[str] = field(default_factory=list)
    player_locations: list[PlayerLocation] = field(default_factory=list)
    damage_type: DamageType = DamageType.UNKNOWN
    weapon_id: str = ""
    weapon_name: str = ""
    ability_slot: AbilitySlot | None = None
    ability_name: str = ""          # resolved via agent + slot
    secondary_fire: bool = False
    killer_side: Side = Side.NONE
    killer_team: str = ""
    victim_team: str = ""

    @property
    def is_utility(self) -> bool:
        return self.damage_type is DamageType.ABILITY

    @property
    def is_teamkill(self) -> bool:
        return bool(self.killer_team) and self.killer_team == self.victim_team


@dataclass(slots=True)
class Plant:
    round_num: int
    round_time_ms: int
    site: str
    location: Point
    planter_puuid: str
    planter_team: str
    player_locations: list[PlayerLocation] = field(default_factory=list)
    # Outcome of the round the plant happened in -- this is what powers the
    # "plant spot win rate" view.
    won: bool = False
    defused: bool = False


@dataclass(slots=True)
class Defuse:
    round_num: int
    round_time_ms: int
    location: Point
    defuser_puuid: str
    defuser_team: str


@dataclass(slots=True)
class Round:
    number: int
    winning_team: str
    result: str                     # "Elimination", "Detonate", "Defuse", ...
    ceremony: str = ""
    plant: Plant | None = None
    defuse: Defuse | None = None
    kills: list[Kill] = field(default_factory=list)
    # side played by each team this round, keyed by team id
    team_sides: dict[str, Side] = field(default_factory=dict)


@dataclass(slots=True)
class MatchMeta:
    match_id: str
    map_id: str                     # asset path, e.g. /Game/Maps/Ascent/Ascent
    map_name: str
    mode: str                       # normalised: "standard", "deathmatch", ...
    mode_raw: str
    queue: str
    started_at: int                 # epoch millis
    game_length_ms: int
    game_version: str = ""
    region: str = ""
    source: str = "local"           # which adapter produced this match
    is_ranked: bool = False


@dataclass(slots=True)
class Match:
    meta: MatchMeta
    players: list[Player]
    rounds: list[Round]
    teams: dict[str, bool] = field(default_factory=dict)  # team id -> won

    _by_puuid: dict[str, Player] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_puuid = {p.puuid: p for p in self.players}

    def player(self, puuid: str) -> Player | None:
        return self._by_puuid.get(puuid)

    @property
    def kills(self) -> list[Kill]:
        return [k for r in self.rounds for k in r.kills]

    @property
    def plants(self) -> list[Plant]:
        return [r.plant for r in self.rounds if r.plant is not None]

    @property
    def is_round_based(self) -> bool:
        """Deathmatch and similar modes have no plants, sides or real rounds."""
        return self.meta.mode not in {"deathmatch", "team_deathmatch", "escalation"}

    def to_summary(self) -> dict[str, Any]:
        return {
            "match_id": self.meta.match_id,
            "map_id": self.meta.map_id,
            "map_name": self.meta.map_name,
            "mode": self.meta.mode,
            "mode_raw": self.meta.mode_raw,
            "queue": self.meta.queue,
            "started_at": self.meta.started_at,
            "game_length_ms": self.meta.game_length_ms,
            "source": self.meta.source,
            "is_ranked": self.meta.is_ranked,
            "is_round_based": self.is_round_based,
            "rounds": len(self.rounds),
            "kills": len(self.kills),
            "plants": len(self.plants),
            "teams": self.teams,
            "players": [
                {
                    "puuid": p.puuid,
                    "name": p.name,
                    "tag": p.tag,
                    "display": p.display,
                    "team": p.team,
                    "agent": p.agent,
                    "agent_id": p.agent_id,
                    "kills": p.kills,
                    "deaths": p.deaths,
                    "assists": p.assists,
                    "score": p.score,
                    "damage": p.damage,
                    "headshots": p.headshots,
                    "bodyshots": p.bodyshots,
                    "legshots": p.legshots,
                }
                for p in self.players
            ],
        }
