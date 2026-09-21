"""Static Valorant reference data: maps, agents, weapons.

Sourced from valorant-api.com and cached on disk so the app runs fully
offline. Refresh with `python -m app.refresh_reference`.

Coordinate transform
--------------------
Riot ships per-map calibration constants. The mapping from world units to
normalised minimap space [0,1] is, empirically (verified against every kill
in the bundled sample matches, 100% in-bounds on all six maps):

    minimap_x = world_y * xMultiplier + xScalarToAdd
    minimap_y = world_x * yMultiplier + yScalarToAdd

Note the axis swap: world *y* drives minimap *x*. This trips up most
community implementations, which is why it is spelled out here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

REF_DIR = Path(__file__).resolve().parent / "data"

# Slot labels vary between sources; fold them onto our canonical ones.
SLOT_ALIASES = {
    "ability1": "ability1",
    "ability_1": "ability1",
    "ability2": "ability2",
    "ability_2": "ability2",
    "grenade": "grenade",
    "grenadeability": "grenade",
    "ability3": "grenade",
    "ultimate": "ultimate",
    "ultimateability": "ultimate",
    "passive": "passive",
}


@dataclass(frozen=True, slots=True)
class MapInfo:
    uuid: str
    name: str
    map_url: str
    minimap: str
    splash: str
    x_multiplier: float
    y_multiplier: float
    x_scalar: float
    y_scalar: float
    callouts: tuple[dict[str, Any], ...] = ()

    @property
    def has_calibration(self) -> bool:
        return self.x_multiplier != 0 and self.y_multiplier != 0

    def to_minimap(self, x: float, y: float) -> tuple[float, float]:
        """World units -> normalised minimap coords in [0, 1]."""
        return (
            y * self.x_multiplier + self.x_scalar,
            x * self.y_multiplier + self.y_scalar,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "map_url": self.map_url,
            "minimap": self.minimap,
            "splash": self.splash,
            "has_calibration": self.has_calibration,
            "callouts": [
                {
                    "region": c.get("regionName", ""),
                    "super_region": c.get("superRegionName", ""),
                    # Callout locations use the same world->minimap transform.
                    "position": dict(
                        zip(
                            ("x", "y"),
                            self.to_minimap(
                                c.get("location", {}).get("x", 0),
                                c.get("location", {}).get("y", 0),
                            ),
                        )
                    ),
                }
                for c in self.callouts
            ],
        }


@dataclass(frozen=True, slots=True)
class AgentInfo:
    uuid: str
    name: str
    role: str
    icon: str
    portrait: str
    # canonical slot -> ability display name
    abilities: dict[str, str]

    def ability_name(self, slot: str) -> str:
        return self.abilities.get(SLOT_ALIASES.get(slot.lower(), slot.lower()), "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "role": self.role,
            "icon": self.icon,
            "portrait": self.portrait,
            "abilities": self.abilities,
        }


@dataclass(frozen=True, slots=True)
class WeaponInfo:
    uuid: str
    name: str
    category: str
    icon: str

    def as_dict(self) -> dict[str, Any]:
        return {"uuid": self.uuid, "name": self.name, "category": self.category, "icon": self.icon}


def _load(filename: str) -> Any:
    path = REF_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Reference data {filename} missing. Run: python -m app.refresh_reference"
        )
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def maps_by_url() -> dict[str, MapInfo]:
    out: dict[str, MapInfo] = {}
    for m in _load("maps.json")["data"]:
        info = MapInfo(
            uuid=m["uuid"],
            name=m["displayName"],
            map_url=m.get("mapUrl") or "",
            minimap=m.get("displayIcon") or "",
            splash=m.get("splash") or "",
            x_multiplier=m.get("xMultiplier") or 0.0,
            y_multiplier=m.get("yMultiplier") or 0.0,
            x_scalar=m.get("xScalarToAdd") or 0.0,
            y_scalar=m.get("yScalarToAdd") or 0.0,
            callouts=tuple(m.get("callouts") or ()),
        )
        if info.map_url:
            out[info.map_url] = info
    return out


@lru_cache(maxsize=1)
def maps_by_name() -> dict[str, MapInfo]:
    return {m.name.lower(): m for m in maps_by_url().values()}


def get_map(map_id: str) -> MapInfo | None:
    """Look up by asset path (Riot) or display name (HenrikDev)."""
    if not map_id:
        return None
    return maps_by_url().get(map_id) or maps_by_name().get(map_id.lower())


@lru_cache(maxsize=1)
def agents_by_id() -> dict[str, AgentInfo]:
    out: dict[str, AgentInfo] = {}
    for a in _load("agents.json")["data"]:
        abilities: dict[str, str] = {}
        for ab in a.get("abilities") or ():
            slot = SLOT_ALIASES.get(str(ab.get("slot", "")).lower())
            if slot and ab.get("displayName"):
                abilities[slot] = ab["displayName"]
        out[a["uuid"].lower()] = AgentInfo(
            uuid=a["uuid"],
            name=a["displayName"],
            role=(a.get("role") or {}).get("displayName", "") if a.get("role") else "",
            icon=a.get("displayIcon") or "",
            portrait=a.get("fullPortrait") or a.get("bustPortrait") or "",
            abilities=abilities,
        )
    return out


@lru_cache(maxsize=1)
def agents_by_name() -> dict[str, AgentInfo]:
    return {a.name.lower(): a for a in agents_by_id().values()}


def get_agent(agent_id: str) -> AgentInfo | None:
    if not agent_id:
        return None
    return agents_by_id().get(agent_id.lower()) or agents_by_name().get(agent_id.lower())


@lru_cache(maxsize=1)
def weapons_by_id() -> dict[str, WeaponInfo]:
    out: dict[str, WeaponInfo] = {}
    for w in _load("weapons.json")["data"]:
        shop = w.get("shopData") or {}
        out[w["uuid"].lower()] = WeaponInfo(
            uuid=w["uuid"],
            name=w["displayName"],
            category=shop.get("categoryText") or _category_from_path(w.get("category", "")),
            icon=w.get("displayIcon") or "",
        )
    return out


def _category_from_path(cat: str) -> str:
    # "EEquippableCategory::Melee" -> "Melee"
    return cat.rsplit("::", 1)[-1] if cat else ""


def get_weapon(weapon_id: str) -> WeaponInfo | None:
    if not weapon_id:
        return None
    by_id = weapons_by_id()
    hit = by_id.get(weapon_id.lower())
    if hit:
        return hit
    lowered = weapon_id.lower()
    for w in by_id.values():
        if w.name.lower() == lowered:
            return w
    return None
