"""Plant analytics, including win rate by clustered plant spot.

The interesting question is not "where do people plant" (a heatmap answers
that) but "which plant spots actually win rounds". Individual plant
coordinates are too sparse to compute a rate per exact point, so nearby
plants are grouped into spots and the rate is computed per group.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import Match, Plant
from ..reference import MapInfo

# Plants within this many world units are treated as the same tactical spot.
# ~800 units is roughly the radius a defuser must contest, so it groups
# "default plant" variants without merging genuinely different positions.
CLUSTER_RADIUS = 800.0
# Below this many plants a rate is too noisy to present as a percentage.
MIN_SAMPLE = 3


@dataclass(slots=True)
class PlantSpot:
    site: str
    plants: list[Plant] = field(default_factory=list)

    @property
    def centroid(self) -> tuple[float, float]:
        n = len(self.plants)
        return (
            sum(p.location.x for p in self.plants) / n,
            sum(p.location.y for p in self.plants) / n,
        )

    @property
    def wins(self) -> int:
        return sum(1 for p in self.plants if p.won)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.plants) if self.plants else 0.0

    @property
    def defused(self) -> int:
        return sum(1 for p in self.plants if p.defused)


def _distance(a, b) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def cluster(plants: Sequence[Plant], radius: float = CLUSTER_RADIUS) -> list[PlantSpot]:
    """Greedy agglomerative clustering, per site.

    Plants are clustered within their own site so an A-site plant can never
    be merged with a B-site one, even on maps where the sites sit close
    together. Seeding from the densest point keeps clusters centred on the
    common spots rather than on outliers.
    """
    spots: list[PlantSpot] = []
    by_site: dict[str, list[Plant]] = {}
    for p in plants:
        by_site.setdefault(p.site or "?", []).append(p)

    for site, group in by_site.items():
        remaining = list(group)
        while remaining:
            # Seed with the plant that has the most neighbours.
            seed = max(
                remaining,
                key=lambda p: sum(
                    1 for q in remaining if _distance(p.location, q.location) <= radius
                ),
            )
            members = [q for q in remaining if _distance(seed.location, q.location) <= radius]
            spots.append(PlantSpot(site=site, plants=members))
            member_ids = {id(m) for m in members}
            remaining = [q for q in remaining if id(q) not in member_ids]

    spots.sort(key=lambda s: len(s.plants), reverse=True)
    return spots


def spot_payload(
    spots: Sequence[PlantSpot],
    map_info: MapInfo,
    min_sample: int = MIN_SAMPLE,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, spot in enumerate(spots):
        cx, cy = spot.centroid
        mx, my = map_info.to_minimap(cx, cy)
        # Spread: how tight the cluster is, in minimap units, for sizing.
        spread = 0.0
        if len(spot.plants) > 1:
            dists = [_distance(spot.plants[0].location, p.location) for p in spot.plants]
            spread = max(dists) * abs(map_info.x_multiplier)
        out.append(
            {
                "id": idx,
                "site": spot.site,
                "position": {"x": mx, "y": my},
                "plants": len(spot.plants),
                "wins": spot.wins,
                "losses": len(spot.plants) - spot.wins,
                "defused": spot.defused,
                "win_rate": round(spot.win_rate, 4),
                # Rates from tiny samples are shown greyed-out in the UI.
                "reliable": len(spot.plants) >= min_sample,
                "spread": round(spread, 4),
                "rounds": sorted(p.round_num for p in spot.plants),
                "avg_plant_time_ms": round(
                    sum(p.round_time_ms for p in spot.plants) / len(spot.plants)
                ),
            }
        )
    return out


def plant_points(plants: Sequence[Plant], map_info: MapInfo) -> list[dict[str, Any]]:
    pts: list[dict[str, Any]] = []
    for p in plants:
        x, y = map_info.to_minimap(p.location.x, p.location.y)
        pts.append(
            {
                "round": p.round_num,
                "site": p.site,
                "t": p.round_time_ms,
                "team": p.planter_team,
                "won": p.won,
                "defused": p.defused,
                "position": {"x": x, "y": y},
            }
        )
    return pts


def site_breakdown(plants: Sequence[Plant]) -> list[dict[str, Any]]:
    by_site: dict[str, list[Plant]] = {}
    for p in plants:
        by_site.setdefault(p.site or "?", []).append(p)
    out = []
    for site, group in sorted(by_site.items()):
        wins = sum(1 for p in group if p.won)
        out.append(
            {
                "site": site,
                "plants": len(group),
                "wins": wins,
                "win_rate": round(wins / len(group), 4) if group else 0.0,
                "defused": sum(1 for p in group if p.defused),
                "avg_plant_time_ms": round(sum(p.round_time_ms for p in group) / len(group)),
            }
        )
    return out


def post_plant_summary(match: Match) -> dict[str, Any]:
    """How much the plant itself is worth, across the selected matches."""
    planted = [r for r in match.rounds if r.plant is not None]
    if not planted:
        return {"planted_rounds": 0, "plant_win_rate": 0.0, "no_plant_win_rate": 0.0}
    plant_wins = sum(1 for r in planted if r.plant and r.plant.won)
    unplanted = [r for r in match.rounds if r.plant is None and r.winning_team]
    # For rounds without a plant, count wins for whichever team attacked.
    atk_wins = 0
    for r in unplanted:
        attacker = next((t for t, s in r.team_sides.items() if s.value == "attack"), None)
        if attacker and r.winning_team == attacker:
            atk_wins += 1
    return {
        "planted_rounds": len(planted),
        "plant_win_rate": round(plant_wins / len(planted), 4),
        "unplanted_rounds": len(unplanted),
        "no_plant_win_rate": round(atk_wins / len(unplanted), 4) if unplanted else 0.0,
    }
