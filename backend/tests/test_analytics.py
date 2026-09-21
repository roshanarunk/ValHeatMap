"""Tests for parsing and the derived stats.

These lock in the behaviour that is easy to get silently wrong: the
coordinate transform, trade detection windows, and plant clustering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.analytics.kills import KillFilters, apply_filters, enrich, summarise
from app.analytics.plants import cluster, spot_payload
from app.models import (
    DamageType, Kill, Match, MatchMeta, Plant, Player, Point, Round, Side,
)
from app.reference import get_agent, get_map
from app.store import MatchStore, parse_any

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "matches"


@pytest.fixture(scope="session")
def loaded_store() -> MatchStore:
    s = MatchStore(DATA_DIR)
    s.load_local()
    return s


# --- reference / coordinates -------------------------------------------
def test_minimap_transform_swaps_axes():
    """World Y drives minimap X. Guards the most error-prone line in the app."""
    ascent = get_map("/Game/Maps/Ascent/Ascent")
    assert ascent is not None
    x, y = ascent.to_minimap(1000, 2000)
    assert x == pytest.approx(2000 * ascent.x_multiplier + ascent.x_scalar)
    assert y == pytest.approx(1000 * ascent.y_multiplier + ascent.y_scalar)


def test_every_sample_kill_lands_on_the_minimap(loaded_store):
    """A regression here means the calibration or the transform broke."""
    for match in loaded_store.all():
        info = get_map(match.meta.map_id)
        assert info is not None and info.has_calibration
        for kill in match.kills:
            mx, my = info.to_minimap(kill.victim_location.x, kill.victim_location.y)
            assert 0.0 <= mx <= 1.0, f"{match.meta.map_name} x={mx}"
            assert 0.0 <= my <= 1.0, f"{match.meta.map_name} y={my}"


def test_agent_ability_slots_resolve():
    jett = get_agent("Jett")
    assert jett is not None
    assert jett.ability_name("ultimate") == "Blade Storm"
    # Riot reports the grenade slot as "GrenadeAbility" in match data.
    assert jett.ability_name("GrenadeAbility") == jett.abilities["grenade"]


# --- parsing ------------------------------------------------------------
def test_loads_all_bundled_matches(loaded_store):
    assert len(loaded_store.all()) == 6


def test_handles_both_riot_field_spellings(loaded_store):
    """Older payloads use `puuid`/`timeSinceRoundStartMillis`."""
    for match in loaded_store.all():
        assert match.players, match.meta.map_name
        assert all(p.puuid for p in match.players)
        for kill in match.kills:
            assert kill.killer_puuid and kill.victim_puuid
            assert kill.time_in_round_ms > 0


def test_killer_locations_mostly_recovered(loaded_store):
    """Killer position is reconstructed from the location snapshot."""
    total = found = 0
    for match in loaded_store.all():
        for kill in match.kills:
            total += 1
            found += kill.killer_location is not None
    assert found / total > 0.95


def test_ability_kills_resolve_to_names(loaded_store):
    named = [
        k for m in loaded_store.all() for k in m.kills
        if k.damage_type is DamageType.ABILITY
    ]
    assert named, "sample data should contain ability kills"
    assert all(k.ability_name for k in named)


def test_plants_have_site_and_planter(loaded_store):
    plants = [p for m in loaded_store.all() for p in m.plants]
    assert plants
    for plant in plants:
        assert plant.site
        # {0,0} is Riot's "no plant" sentinel and must never survive parsing.
        assert not (plant.location.x == 0 and plant.location.y == 0)


def test_sides_alternate_at_the_half(loaded_store):
    match = next(m for m in loaded_store.all() if len(m.players) == 10)
    first = next(r for r in match.rounds if r.number == 0)
    twelfth = next((r for r in match.rounds if r.number == 12), None)
    if twelfth is None or not first.team_sides:
        pytest.skip("match too short to cover a side swap")
    for team, side in first.team_sides.items():
        assert twelfth.team_sides[team] != side


def test_rejects_unknown_payload():
    with pytest.raises(ValueError):
        parse_any({"nonsense": True})


# --- mode awareness -----------------------------------------------------
def _as_deathmatch() -> dict:
    """Re-label a bundled match as deathmatch to exercise the mode path."""
    with (DATA_DIR / "example_ascent_game.json").open(encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["matchInfo"]["matchId"] = "synthetic-dm"
    payload["matchInfo"]["gameMode"] = (
        "/Game/GameModes/Deathmatch/DeathmatchGameMode.DeathmatchGameMode_C"
    )
    return payload


def test_deathmatch_has_no_sides_plants_or_openings():
    match = parse_any(_as_deathmatch())
    assert match.meta.mode == "deathmatch"
    assert match.is_round_based is False
    assert match.plants == []
    assert {k.killer_side for k in match.kills} == {Side.NONE}
    # Every DM kill would otherwise look like an opening kill.
    assert not any(ek.first_blood for ek in enrich(match))


def test_deathmatch_kills_still_map_to_minimap():
    match = parse_any(_as_deathmatch())
    info = get_map(match.meta.map_id)
    assert info is not None
    for kill in match.kills:
        mx, my = info.to_minimap(kill.victim_location.x, kill.victim_location.y)
        assert 0.0 <= mx <= 1.0 and 0.0 <= my <= 1.0


# --- trade detection ----------------------------------------------------
def _trade_fixture(gap_ms: int, distance: float, same_team: bool = True) -> Match:
    """A rides B, B dies, B's teammate kills A `gap_ms` later."""
    meta = MatchMeta(
        match_id="t", map_id="/Game/Maps/Ascent/Ascent", map_name="Ascent",
        mode="standard", mode_raw="Bomb", queue="unrated",
        started_at=0, game_length_ms=0,
    )
    players = [
        Player(puuid="A", name="A", tag="1", team="Red", agent="Jett", agent_id=""),
        Player(puuid="B", name="B", tag="2", team="Blue", agent="Sage", agent_id=""),
        Player(puuid="C", name="C", tag="3", team="Blue" if same_team else "Red",
               agent="Sova", agent_id=""),
    ]
    first = Kill(
        round_num=0, time_in_round_ms=10_000, time_in_match_ms=10_000,
        killer_puuid="A", victim_puuid="B",
        victim_location=Point(0, 0), killer_location=Point(500, 0),
        killer_team="Red", victim_team="Blue",
    )
    revenge = Kill(
        round_num=0, time_in_round_ms=10_000 + gap_ms, time_in_match_ms=10_000 + gap_ms,
        killer_puuid="C", victim_puuid="A",
        victim_location=Point(distance, 0), killer_location=Point(distance, 500),
        killer_team="Blue" if same_team else "Red", victim_team="Red",
    )
    rnd = Round(number=0, winning_team="Blue", result="Elimination", kills=[first, revenge])
    return Match(meta=meta, players=players, rounds=[rnd], teams={"Red": False, "Blue": True})


def test_trade_detected_within_window_and_radius():
    enriched = enrich(_trade_fixture(gap_ms=1500, distance=800))
    assert enriched[0].traded is True
    assert enriched[1].trade_kill is True
    assert enriched[1].trade_latency_ms == 1500


def test_no_trade_outside_time_window():
    enriched = enrich(_trade_fixture(gap_ms=8000, distance=800))
    assert enriched[0].traded is False


def test_no_trade_outside_radius():
    """A revenge kill across the map is not a trade."""
    enriched = enrich(_trade_fixture(gap_ms=1500, distance=50_000))
    assert enriched[0].traded is False


def test_no_trade_when_avenger_is_not_a_teammate():
    enriched = enrich(_trade_fixture(gap_ms=1500, distance=800, same_team=False))
    assert enriched[0].traded is False


def test_first_blood_is_first_kill_of_round(loaded_store):
    for match in loaded_store.all():
        for rnd in match.rounds:
            if not rnd.kills:
                continue
            enriched = [e for e in enrich(match) if e.kill.round_num == rnd.number]
            assert sum(1 for e in enriched if e.first_blood) == 1


def test_post_plant_kills_follow_the_plant(loaded_store):
    match = next(m for m in loaded_store.all() if m.plants)
    for ek in enrich(match):
        rnd = next(r for r in match.rounds if r.number == ek.kill.round_num)
        if rnd.plant is None:
            assert ek.post_plant is False
        elif ek.post_plant:
            assert ek.kill.time_in_round_ms >= rnd.plant.round_time_ms


# --- filtering ----------------------------------------------------------
def test_filters_narrow_the_selection(loaded_store):
    match = next(m for m in loaded_store.all() if len(m.kills) > 50)
    enriched = enrich(match)
    agent = enriched[0].killer_agent
    filtered = apply_filters(enriched, KillFilters(agents=frozenset({agent})))
    assert filtered
    assert all(e.killer_agent == agent for e in filtered)
    assert len(filtered) < len(enriched)


def test_time_window_filter_is_inclusive(loaded_store):
    match = next(m for m in loaded_store.all() if len(m.kills) > 50)
    enriched = enrich(match)
    filtered = apply_filters(enriched, KillFilters(time_start_ms=0, time_end_ms=20_000))
    assert all(0 <= e.kill.time_in_round_ms <= 20_000 for e in filtered)


def test_query_parser_reads_csv_and_flags():
    f = KillFilters.from_query(
        {"agents": "Jett,Sova", "sides": "attack", "time_end": "30000", "traded_only": "1"}
    )
    assert f.agents == frozenset({"Jett", "Sova"})
    assert f.sides == frozenset({"attack"})
    assert f.time_end_ms == 30_000
    assert f.traded_only is True
    assert f.exclude_teamkills is True


def test_teamkills_excluded_by_default(loaded_store):
    for match in loaded_store.all():
        filtered = apply_filters(enrich(match), KillFilters())
        assert not any(e.kill.is_teamkill for e in filtered)


def test_summary_rates_are_fractions(loaded_store):
    match = next(m for m in loaded_store.all() if len(m.kills) > 50)
    stats = summarise(enrich(match), match)
    assert stats["total"] > 0
    for key in ("trade_rate", "utility_rate", "round_win_rate"):
        assert 0.0 <= stats[key] <= 1.0


def test_summary_of_empty_selection_is_safe():
    meta = MatchMeta("x", "", "", "standard", "", "", 0, 0)
    empty = Match(meta=meta, players=[], rounds=[])
    assert summarise([], empty)["total"] == 0


# --- plant clustering ---------------------------------------------------
def _plant(x: float, y: float, site: str, won: bool, rnd: int = 0) -> Plant:
    return Plant(
        round_num=rnd, round_time_ms=40_000, site=site, location=Point(x, y),
        planter_puuid="p", planter_team="Red", won=won,
    )


def test_cluster_groups_nearby_plants():
    spots = cluster(
        [_plant(0, 0, "A", True), _plant(100, 100, "A", False), _plant(9000, 9000, "A", True)],
        radius=800,
    )
    assert len(spots) == 2
    assert sorted(len(s.plants) for s in spots) == [1, 2]


def test_cluster_never_merges_across_sites():
    """Identical coordinates on different sites stay separate."""
    spots = cluster([_plant(0, 0, "A", True), _plant(0, 0, "B", False)], radius=5000)
    assert len(spots) == 2
    assert {s.site for s in spots} == {"A", "B"}


def test_win_rate_and_reliability_flag():
    spots = cluster([_plant(i * 10, 0, "A", won=i < 3, rnd=i) for i in range(4)], radius=800)
    payload = spot_payload(spots, get_map("/Game/Maps/Ascent/Ascent"), min_sample=3)
    assert payload[0]["plants"] == 4
    assert payload[0]["win_rate"] == 0.75
    assert payload[0]["reliable"] is True
    # A single plant is too small a sample to trust.
    thin = spot_payload(cluster([_plant(0, 0, "B", True)]), get_map("/Game/Maps/Ascent/Ascent"))
    assert thin[0]["reliable"] is False


def test_plant_win_rate_matches_round_outcome(loaded_store):
    """`won` must agree with the round result recorded for that round."""
    for match in loaded_store.all():
        for rnd in match.rounds:
            if rnd.plant and rnd.plant.planter_team:
                assert rnd.plant.won == (rnd.winning_team == rnd.plant.planter_team)


# --- HenrikDev adapter --------------------------------------------------
def _henrik_payload() -> dict:
    """A minimal but schema-accurate HenrikDev v4 match.

    Mirrors the shapes documented at docs.henrikdev.xyz: flat `kills` tagged
    with a round number, plants hanging off each round, and pre-resolved
    {id, name} objects for agents and weapons.
    """
    def ref(puuid: str, team: str) -> dict:
        return {"puuid": puuid, "name": puuid.upper(), "tag": "000", "team": team}

    return {
        "status": 200,
        "data": {
            "metadata": {
                "match_id": "henrik-1",
                "map": {"id": "m", "name": "Ascent"},
                "game_version": "release-09",
                "game_length_in_ms": 1000,
                "started_at": "2024-05-01T10:00:00Z",
                "queue": {"id": "competitive", "name": "Competitive", "mode_type": "Competitive"},
                "region": "eu",
            },
            "players": [
                {
                    "puuid": "atk", "name": "ATK", "tag": "1", "team_id": "red",
                    "agent": {"id": "add6443a-41bd-e414-f6ad-e58d267f4e95", "name": "Jett"},
                    "stats": {"kills": 1, "deaths": 0, "assists": 0, "score": 200,
                              "headshots": 1, "bodyshots": 0, "legshots": 0,
                              "damage": {"dealt": 150, "received": 0}},
                },
                {
                    "puuid": "def", "name": "DEF", "tag": "2", "team_id": "blue",
                    "agent": {"id": "320b2a48-4d9b-a075-30f1-1f93a9b638fa", "name": "Sova"},
                    "stats": {"kills": 0, "deaths": 1, "assists": 0, "score": 0,
                              "headshots": 0, "bodyshots": 0, "legshots": 0,
                              "damage": {"dealt": 0, "received": 150}},
                },
            ],
            "teams": [
                {"team_id": "red", "won": True, "rounds": {"won": 13, "lost": 5}},
                {"team_id": "blue", "won": False, "rounds": {"won": 5, "lost": 13}},
            ],
            "kills": [
                {
                    "round": 0,
                    "time_in_round_in_ms": 30000,
                    "time_in_match_in_ms": 60000,
                    "killer": ref("atk", "red"),
                    "victim": ref("def", "blue"),
                    "assistants": [],
                    "location": {"x": 1000, "y": 2000},
                    "weapon": {"id": "ability", "name": "Ultimate", "type": "Ability"},
                    "secondary_fire_mode": False,
                    "player_locations": [
                        {"player": ref("atk", "red"), "view_radians": 1.0,
                         "location": {"x": 1500, "y": 2500}},
                    ],
                },
            ],
            "rounds": [
                {
                    "id": 0,
                    "result": "Detonate",
                    "ceremony": "",
                    "winning_team": "red",
                    "plant": {
                        "round_time_in_ms": 45000,
                        "site": "A",
                        "location": {"x": 500, "y": 900},
                        "player": ref("atk", "red"),
                        "player_locations": [],
                    },
                    "defuse": None,
                    "stats": [],
                },
            ],
        },
    }


def test_henrik_payload_normalises_like_riot():
    match = parse_any(_henrik_payload())
    assert match.meta.source == "henrik"
    assert match.meta.match_id == "henrik-1"
    assert match.meta.map_name == "Ascent"
    # Teams normalise to Riot's capitalisation so both sources compare.
    assert set(match.teams) == {"Red", "Blue"}
    assert [p.agent for p in match.players] == ["Jett", "Sova"]


def test_henrik_kill_resolves_ability_and_killer_position():
    match = parse_any(_henrik_payload())
    kill = match.kills[0]
    assert kill.damage_type is DamageType.ABILITY
    # "Ultimate" + Jett must resolve to the real ability name.
    assert kill.ability_name == "Blade Storm"
    assert kill.killer_location == Point(1500.0, 2500.0)
    assert kill.killer_team == "Red"


def test_henrik_plant_carries_round_outcome():
    match = parse_any(_henrik_payload())
    plant = match.plants[0]
    assert plant.site == "A"
    assert plant.planter_team == "Red"
    # Red planted and Red won the round.
    assert plant.won is True


def test_henrik_accepts_inner_object_without_envelope():
    inner = _henrik_payload()["data"]
    assert parse_any(inner).meta.match_id == "henrik-1"
