"""Tests for the analytics database and its query layer.

These run against a temporary database built from synthetic matches, so
they neither touch nor depend on the real crawled dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analytics_db import (
    FLAG_FIRST_BLOOD,
    FLAG_TRADED,
    AnalyticsDB,
    _act_sort_key,
    _avg_tier,
    _patch_of,
)
from app.analytics.kills import enrich
from app.models import (
    DamageType, Kill, Match, MatchMeta, Plant, Player, Point, Round, Side,
)
from app.queries import Filters, QueryEngine
from app.reference import get_map


def _world_at(map_name: str, mx: float, my: float) -> Point:
    """World coordinates that land at a given minimap position.

    Inverts the calibration rather than hardcoding numbers, so the fixtures
    stay inside the map however the reference data changes.
    """
    info = get_map(map_name)
    assert info is not None
    return Point(
        (my - info.y_scalar) / info.y_multiplier,
        (mx - info.x_scalar) / info.x_multiplier,
    )


def _match(
    match_id: str,
    map_name: str = "Ascent",
    act: str = "e11a5",
    tier: int = 25,
    kills: int = 4,
) -> Match:
    meta = MatchMeta(
        match_id=match_id,
        map_id="/Game/Maps/Ascent/Ascent",
        map_name=map_name,
        mode="standard",
        mode_raw="Bomb",
        queue="competitive",
        started_at=1_700_000_000_000,
        game_length_ms=1_800_000,
        game_version="release-13.05-shipping-11-5350494",
        region="na",
        act=act,
    )
    players = [
        Player(puuid="atk", name="Atk", tag="1", team="Red", agent="Jett", agent_id="", tier=tier),
        Player(puuid="def", name="Def", tag="2", team="Blue", agent="Sage", agent_id="", tier=tier),
    ]
    round_kills = []
    for i in range(kills):
        round_kills.append(
            Kill(
                round_num=0,
                time_in_round_ms=10_000 + i * 5_000,
                time_in_match_ms=10_000 + i * 5_000,
                killer_puuid="atk" if i % 2 == 0 else "def",
                victim_puuid="def" if i % 2 == 0 else "atk",
                # Spread the points across distinct map cells, well inside
                # the calibrated bounds.
                victim_location=_world_at(map_name, 0.3 + i * 0.1, 0.35 + i * 0.08),
                killer_location=_world_at(map_name, 0.35 + i * 0.1, 0.4 + i * 0.08),
                damage_type=DamageType.ABILITY if i == 0 else DamageType.WEAPON,
                weapon_name="" if i == 0 else "Vandal",
                ability_name="Blade Storm" if i == 0 else "",
                killer_side=Side.ATTACK if i % 2 == 0 else Side.DEFENSE,
                killer_team="Red" if i % 2 == 0 else "Blue",
                victim_team="Blue" if i % 2 == 0 else "Red",
            )
        )
    plant = Plant(
        round_num=0, round_time_ms=40_000, site="A",
        location=_world_at(map_name, 0.42, 0.55), planter_puuid="atk", planter_team="Red",
        won=True, defused=False,
    )
    rnd = Round(
        number=0, winning_team="Red", result="Detonate",
        plant=plant, kills=round_kills,
        team_sides={"Red": Side.ATTACK, "Blue": Side.DEFENSE},
    )
    return Match(meta=meta, players=players, rounds=[rnd], teams={"Red": True, "Blue": False})


@pytest.fixture()
def db(tmp_path: Path) -> AnalyticsDB:
    database = AnalyticsDB(tmp_path / "a.db")
    info = get_map("Ascent")
    for i in range(6):
        m = _match(f"m{i}", tier=27 if i < 2 else 22, act="e11a5" if i < 4 else "e11a4")
        database.add_match(m, info, enrich(m))
    return database


@pytest.fixture()
def engine(db: AnalyticsDB) -> QueryEngine:
    return QueryEngine(db)


# --- derivation ---------------------------------------------------------
def test_patch_parsed_from_game_version():
    assert _patch_of("release-13.05-shipping-11-5350494") == "13.05"
    assert _patch_of("") == ""


def test_avg_tier_is_mean_of_players():
    m = _match("x", tier=24)
    assert _avg_tier(m) == 24


def test_acts_sort_by_episode_then_act():
    """String sort puts e9a3 after e11a5; these must order numerically."""
    acts = ["e9a3", "e11a5", "e10a1", "e11a2"]
    assert sorted(acts, key=_act_sort_key, reverse=True) == [
        "e11a5", "e11a2", "e10a1", "e9a3",
    ]


# --- storage ------------------------------------------------------------
def test_stores_matches_kills_and_plants(db: AnalyticsDB):
    stats = db.stats()
    assert stats["matches"] == 6
    assert stats["kills"] == 24  # 6 matches x 4 kills
    assert stats["plants"] == 6


def test_reingesting_replaces_rather_than_duplicates(db: AnalyticsDB):
    before = db.stats()["kills"]
    m = _match("m0")
    db.add_match(m, get_map("Ascent"), enrich(m))
    assert db.stats()["kills"] == before


def test_names_are_interned_to_small_ids(db: AnalyticsDB):
    """Text repeated per row is what made the first schema 2.7x larger."""
    maps = db.dim_ids("map")
    agents = db.dim_ids("agent")
    assert maps["Ascent"] == 1
    assert set(agents) == {"Jett", "Sage"}
    assert all(isinstance(v, int) for v in agents.values())


def test_positions_stored_in_minimap_space(db: AnalyticsDB):
    with db.connect() as conn:
        rows = conn.execute("SELECT vx, vy FROM kills").fetchall()
    assert rows
    for r in rows:
        assert 0.0 <= r["vx"] <= 1.0
        assert 0.0 <= r["vy"] <= 1.0


def test_flags_pack_kill_context(db: AnalyticsDB):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT flags FROM kills WHERE t_ms = 10000 LIMIT 1"
        ).fetchone()
    assert row["flags"] & FLAG_FIRST_BLOOD


# --- filtering ----------------------------------------------------------
def test_map_filter_is_required_to_narrow(engine: QueryEngine):
    all_kills = engine.summary(Filters(map_name="Ascent"))["total"]
    assert all_kills == 24
    assert engine.summary(Filters(map_name="Nonexistent"))["total"] == 0


def test_rank_band_filter(engine: QueryEngine):
    """Radiant band is tier 27; only the first two matches qualify."""
    radiant = engine.summary(Filters(map_name="Ascent", ranks=["radiant"]))
    assert radiant["total"] == 8
    assert radiant["matches"] == 2


def test_act_filter(engine: QueryEngine):
    current = engine.summary(Filters(map_name="Ascent", acts=["e11a5"]))
    assert current["matches"] == 4


def test_agent_filter_uses_killer(engine: QueryEngine):
    jett = engine.summary(Filters(map_name="Ascent", agents=["Jett"]))
    # Jett kills on even indices: 2 of every 4.
    assert jett["total"] == 12


def test_side_filter(engine: QueryEngine):
    atk = engine.summary(Filters(map_name="Ascent", sides=["attack"]))
    dfn = engine.summary(Filters(map_name="Ascent", sides=["defense"]))
    assert atk["total"] + dfn["total"] == 24


def test_time_window_filter(engine: QueryEngine):
    early = engine.kill_points(Filters(map_name="Ascent", time_end=12_000))
    assert early["total"] == 6  # only the first kill of each match
    assert all(p["t"] <= 12_000 for p in early["points"])


def test_utility_filter_matches_ability_kills(engine: QueryEngine):
    util = engine.summary(Filters(map_name="Ascent", utility_only=True))
    assert util["total"] == 6  # one ability kill per match
    rows = engine.ability_breakdown(Filters(map_name="Ascent"))
    assert rows[0]["ability"] == "Blade Storm"
    assert rows[0]["agent"] == "Jett"


def test_filters_compose(engine: QueryEngine):
    combined = engine.summary(
        Filters(map_name="Ascent", ranks=["radiant"], acts=["e11a5"], agents=["Jett"])
    )
    assert 0 < combined["total"] <= 8


def test_unknown_map_short_circuits(engine: QueryEngine):
    """An unknown map must not fall through to a full table scan."""
    assert engine.kill_points(Filters(map_name="Nope"))["points"] == []


# --- query output -------------------------------------------------------
def test_kill_points_resolve_names_and_positions(engine: QueryEngine):
    result = engine.kill_points(Filters(map_name="Ascent"))
    p = result["points"][0]
    assert p["killer_agent"] in {"Jett", "Sage"}
    assert p["side"] in {"attack", "defense"}
    assert 0 <= p["victim_pos"]["x"] <= 1
    assert p["killer_pos"] is not None


def test_large_selections_are_sampled_deterministically(engine: QueryEngine):
    """Above the limit the API returns a stable uniform subset."""
    f = Filters(map_name="Ascent", limit=5)
    first = engine.kill_points(f)
    second = engine.kill_points(f)
    assert first["total"] == 24
    assert first["sampled"] is True
    assert len(first["points"]) <= 24
    assert [p["t"] for p in first["points"]] == [p["t"] for p in second["points"]]


def test_histogram_buckets_by_time(engine: QueryEngine):
    hist = engine.histogram(Filters(map_name="Ascent"), bucket_ms=5000)
    assert hist
    assert sum(h["count"] for h in hist) == 24
    assert all(h["t"] % 5000 == 0 for h in hist)


def test_plants_query_returns_positions_and_outcome(engine: QueryEngine):
    plants = engine.plants(Filters(map_name="Ascent"))
    assert len(plants) == 6
    assert all(p["site"] == "A" for p in plants)
    assert all(0 <= p["position"]["x"] <= 1 for p in plants)
    assert all(p["won"] for p in plants)


def test_facets_list_available_filter_values(db: AnalyticsDB):
    facets = db.facets()
    assert facets["maps"][0]["map_name"] == "Ascent"
    assert [a["act"] for a in facets["acts"]] == ["e11a5", "e11a4"]
    assert {a["agent"] for a in facets["agents"]} == {"Jett", "Sage"}
    assert facets["tier_range"] == [22, 27]


def test_summary_rates_are_fractions(engine: QueryEngine):
    s = engine.summary(Filters(map_name="Ascent"))
    assert 0.0 <= s["trade_rate"] <= 1.0
    assert 0.0 <= s["utility_rate"] <= 1.0


def test_engine_cache_can_be_invalidated(db: AnalyticsDB):
    """A newly seen agent must appear after an ingest, not stay cached."""
    engine = QueryEngine(db)
    assert engine.summary(Filters(map_name="Ascent", agents=["Omen"]))["total"] == 0

    # Build a match whose killer really is Omen, so enrich() records it.
    m = _match("new")
    m.players[0].agent = "Omen"
    db.add_match(m, get_map("Ascent"), enrich(m))

    engine.invalidate()
    assert engine.summary(Filters(map_name="Ascent", agents=["Omen"]))["total"] > 0


def test_unresolvable_filter_matches_nothing_not_everything(engine: QueryEngine):
    """A filter naming only unknown values must return no rows.

    Dropping such a filter silently returns the entire dataset, which looks
    to a user exactly like the filter being ignored.
    """
    assert engine.summary(Filters(map_name="Ascent", agents=["Nobody"]))["total"] == 0
    assert engine.summary(Filters(map_name="Ascent", acts=["e1a1"]))["total"] == 0
    assert engine.summary(Filters(map_name="Ascent", weapons=["Trombone"]))["total"] == 0
    # A mix of known and unknown keeps the known part.
    mixed = engine.summary(Filters(map_name="Ascent", agents=["Jett", "Nobody"]))
    assert mixed["total"] > 0
