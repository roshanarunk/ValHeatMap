"""Unit tests for Macro Rotation Flow analytics, extraction, and queries."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.analytics.rotations import extract_rotations, get_map_zones, macro_zone_for_callout
from app.analytics_db import AnalyticsDB
from app.main import app
from app.models import Kill, Match, MatchMeta, Player, PlayerLocation, Point, Round, Side
from app.queries import QueryEngine
from app.reference import get_map


def test_macro_zone_mapping():
    assert macro_zone_for_callout("ascent", "Defender Side", "Spawn") == "CT Spawn"
    assert macro_zone_for_callout("ascent", "Attacker Side", "Spawn") == "T Spawn"
    assert macro_zone_for_callout("ascent", "A", "Tree") == "A Tree"
    assert macro_zone_for_callout("ascent", "A", "Garden") == "A Tree"
    assert macro_zone_for_callout("ascent", "Mid", "Market") == "Market"
    assert macro_zone_for_callout("ascent", "A", "Window") == "A Heaven"
    assert macro_zone_for_callout("ascent", "A", "Site") == "A Site"
    assert macro_zone_for_callout("ascent", "B", "Site") == "B Site"
    assert macro_zone_for_callout("haven", "C", "Garage") == "Garage"
    assert macro_zone_for_callout("bind", "B", "Window") == "B Hookah"
    assert macro_zone_for_callout("split", "Mid", "Mail") == "Mail"


def test_get_map_zones():
    zones = get_map_zones("Ascent")
    assert len(zones) >= 8
    for zid, z in zones.items():
        assert "x" in z and "y" in z
        assert 0.0 <= z["x"] <= 1.0
        assert 0.0 <= z["y"] <= 1.0
        assert z["name"] == zid


def test_extract_rotations_synthetic():
    map_info = get_map("Ascent")
    assert map_info is not None

    # Callouts:
    # B Site: (-2344, -7548)
    # Market: (1089, -7363)
    # A Site: (6153, -6626)
    p1 = Player(
        puuid="p1",
        name="Tester",
        tag="NA1",
        team="Blue",
        agent="Killjoy",
        agent_id="kj-uuid",
    )
    p2 = Player(
        puuid="p2",
        name="Enemy",
        tag="NA1",
        team="Red",
        agent="Jett",
        agent_id="jett-uuid",
    )

    # 3 kills at t=10s, t=25s, t=40s
    # p1 is observed at B Site at 10s, at Market at 25s, and at A Site at 40s
    loc10 = [PlayerLocation("p1", Point(-2344.0, -7548.0))]
    loc25 = [PlayerLocation("p1", Point(1089.0, -7363.0))]
    loc40 = [PlayerLocation("p1", Point(6153.0, -6626.0))]

    k1 = Kill(
        round_num=1,
        time_in_round_ms=10000,
        time_in_match_ms=10000,
        killer_puuid="p2",
        victim_puuid="p3",
        victim_location=Point(0, 0),
        killer_location=Point(0, 0),
        player_locations=loc10,
    )
    k2 = Kill(
        round_num=1,
        time_in_round_ms=25000,
        time_in_match_ms=25000,
        killer_puuid="p2",
        victim_puuid="p4",
        victim_location=Point(0, 0),
        killer_location=Point(0, 0),
        player_locations=loc25,
    )
    k3 = Kill(
        round_num=1,
        time_in_round_ms=40000,
        time_in_match_ms=40000,
        killer_puuid="p1",
        victim_puuid="p2",
        victim_location=Point(6153.0, -6626.0),
        killer_location=Point(6153.0, -6626.0),
        player_locations=loc40,
    )

    rd = Round(
        number=1,
        winning_team="Blue",
        result="Elimination",
        kills=[k1, k2, k3],
        team_sides={"Blue": Side.DEFENSE, "Red": Side.ATTACK},
    )

    meta = MatchMeta(
        match_id="test-match-rot",
        map_id="/Game/Maps/Ascent/Ascent",
        map_name="Ascent",
        mode="standard",
        mode_raw="unrated",
        queue="unrated",
        started_at=1700000000000,
        game_length_ms=100000,
    )
    match = Match(meta=meta, players=[p1, p2], rounds=[rd])

    rotations = extract_rotations(match, map_info)
    assert len(rotations) == 3

    p1_rotations = [r for r in rotations if r.player_puuid == "p1"]
    assert len(p1_rotations) == 2

    # First rotation: B Site -> Market
    r1 = p1_rotations[0]
    assert r1.player_puuid == "p1"
    assert r1.agent == "Killjoy"
    assert r1.team == "Blue"
    assert r1.from_zone == "B Site"
    assert r1.to_zone == "Market"
    assert r1.t_start_ms == 10000
    assert r1.t_end_ms == 25000
    assert r1.side == 2  # Defense
    assert r1.won == 1

    # Second rotation: Market -> A Site
    r2 = p1_rotations[1]
    assert r2.player_puuid == "p1"
    assert r2.agent == "Killjoy"
    assert r2.team == "Blue"
    assert r2.from_zone == "Market"
    assert r2.to_zone == "A Site"
    assert r2.t_start_ms == 25000
    assert r2.t_end_ms == 40000
    assert r2.side == 2
    assert r2.won == 1


def test_query_engine_rotations(tmp_path):
    map_info = get_map("Ascent")
    assert map_info is not None

    db = AnalyticsDB(tmp_path / "test.db")
    engine = QueryEngine(db)

    # Empty database
    res = engine.rotations("Ascent", side="defense")
    assert res["map_name"] == "Ascent"
    assert res["total_transitions"] == 0
    assert len(res["zones"]) >= 8

    # Create synthetic match and insert
    p1 = Player(puuid="p1", name="Tester", tag="NA1", team="Blue", agent="Killjoy", agent_id="kj")
    p2 = Player(puuid="p2", name="Enemy", tag="NA1", team="Red", agent="Jett", agent_id="jett")
    loc1 = [PlayerLocation("p1", Point(-2344.0, -7548.0))]
    loc2 = [PlayerLocation("p1", Point(1089.0, -7363.0))]
    k1 = Kill(round_num=1, time_in_round_ms=10000, time_in_match_ms=10000, killer_puuid="p2", victim_puuid="p3",
              victim_location=Point(0,0), killer_location=Point(0,0), player_locations=loc1)
    k2 = Kill(round_num=1, time_in_round_ms=25000, time_in_match_ms=25000, killer_puuid="p1", victim_puuid="p2",
              victim_location=Point(1089.0,-7363.0), killer_location=Point(1089.0,-7363.0), player_locations=loc2)
    rd = Round(number=1, winning_team="Blue", result="Elimination", kills=[k1, k2],
               team_sides={"Blue": Side.DEFENSE, "Red": Side.ATTACK})
    meta = MatchMeta(match_id="match-123", map_id="/Game/Maps/Ascent/Ascent", map_name="Ascent",
                     mode="standard", mode_raw="unrated", queue="unrated", started_at=1700000000000, game_length_ms=50000)
    match = Match(meta=meta, players=[p1, p2], rounds=[rd])
    db.add_match(match, map_info)

    # Query with agent filter
    kj_res = engine.rotations("Ascent", side="defense", player="p1", agent="Killjoy")
    assert kj_res["total_transitions"] == 1
    assert kj_res["transitions"][0]["from_zone"] == "B Site"
    assert kj_res["transitions"][0]["to_zone"] == "Market"
    assert "Killjoy" in kj_res["available_agents"]

    # Query with non-matching agent
    jett_res = engine.rotations("Ascent", side="defense", player="p1", agent="Jett")
    assert jett_res["total_transitions"] == 0

    # Query by match_id and team
    match_res = engine.rotations(match_id="match-123", team="Blue")
    assert match_res["total_transitions"] == 1
    assert match_res["match_id"] == "match-123"
    assert "match_players" in match_res
    assert len(match_res["match_players"]) == 2
    blue_p = [p for p in match_res["match_players"] if p["team"] == "Blue"][0]
    red_p = [p for p in match_res["match_players"] if p["team"] == "Red"][0]
    assert blue_p["agent"] == "Killjoy"
    assert blue_p["role"] == "Sentinel"
    assert blue_p["icon"] != ""
    assert red_p["agent"] == "Jett"
    assert red_p["role"] == "Duelist"
    assert red_p["icon"] != ""

    # Query by match_id and player
    p1_match_res = engine.rotations(match_id="match-123", player="p1")
    assert p1_match_res["total_transitions"] == 1

    p2_match_res = engine.rotations(match_id="match-123", player="non-existent")
    assert p2_match_res["total_transitions"] == 0

    enemy_match_res = engine.rotations(match_id="match-123", team="Red")
    assert enemy_match_res["total_transitions"] == 0


def test_api_rotations_endpoint():
    client = TestClient(app)
    resp = client.get("/api/rotations?map_name=Ascent&side=defense")
    assert resp.status_code == 200
    data = resp.json()
    assert data["map_name"] == "Ascent"
    assert "zones" in data
    assert "transitions" in data
    assert "available_agents" in data


def test_player_search_rotations_backfills_missing_matches(tmp_path, monkeypatch):
    """Test that searching rotations for a player backfills missing matches and lists all played agents."""
    map_info = get_map("Ascent")
    assert map_info is not None

    db = AnalyticsDB(tmp_path / "test.db")
    engine = QueryEngine(db)

    # Match 1: p1 played Killjoy
    p1 = Player(puuid="p1", name="Tester", tag="NA1", team="Blue", agent="Killjoy", agent_id="kj")
    p2 = Player(puuid="p2", name="Enemy", tag="NA1", team="Red", agent="Jett", agent_id="jett")
    loc1 = [PlayerLocation("p1", Point(-2344.0, -7548.0))]
    loc2 = [PlayerLocation("p1", Point(1089.0, -7363.0))]
    k1 = Kill(round_num=1, time_in_round_ms=10000, time_in_match_ms=10000, killer_puuid="p2", victim_puuid="p3",
              victim_location=Point(0,0), killer_location=Point(0,0), player_locations=loc1)
    k2 = Kill(round_num=1, time_in_round_ms=25000, time_in_match_ms=25000, killer_puuid="p1", victim_puuid="p2",
              victim_location=Point(1089.0,-7363.0), killer_location=Point(1089.0,-7363.0), player_locations=loc2)
    rd1 = Round(number=1, winning_team="Blue", result="Elimination", kills=[k1, k2],
                team_sides={"Blue": Side.DEFENSE, "Red": Side.ATTACK})
    meta1 = MatchMeta(match_id="match-kj", map_id="/Game/Maps/Ascent/Ascent", map_name="Ascent",
                      mode="standard", mode_raw="unrated", queue="unrated", started_at=1700000000000, game_length_ms=50000)
    match1 = Match(meta=meta1, players=[p1, p2], rounds=[rd1])
    db.add_match(match1, map_info)

    # Match 2: p1 played Waylay
    p1_waylay = Player(puuid="p1", name="Tester", tag="NA1", team="Blue", agent="Waylay", agent_id="waylay")
    loc3 = [PlayerLocation("p1", Point(6153.0, -6626.0))]
    loc4 = [PlayerLocation("p1", Point(1089.0, -7363.0))]
    k3 = Kill(round_num=1, time_in_round_ms=10000, time_in_match_ms=10000, killer_puuid="p2", victim_puuid="p4",
              victim_location=Point(0,0), killer_location=Point(0,0), player_locations=loc3)
    k4 = Kill(round_num=1, time_in_round_ms=25000, time_in_match_ms=25000, killer_puuid="p1", victim_puuid="p2",
              victim_location=Point(1089.0,-7363.0), killer_location=Point(1089.0,-7363.0), player_locations=loc4)
    rd2 = Round(number=1, winning_team="Blue", result="Elimination", kills=[k3, k4],
                team_sides={"Blue": Side.DEFENSE, "Red": Side.ATTACK})
    meta2 = MatchMeta(match_id="match-waylay", map_id="/Game/Maps/Ascent/Ascent", map_name="Ascent",
                      mode="standard", mode_raw="unrated", queue="unrated", started_at=1700000050000, game_length_ms=50000)
    match2 = Match(meta=meta2, players=[p1_waylay, p2], rounds=[rd2])
    db.add_match(match2, map_info)

    # Delete Match 2 from rotations table to simulate it not yet being backfilled
    with db.connect() as conn:
        m2_id = conn.execute("SELECT id FROM matches WHERE match_id = 'match-waylay'").fetchone()["id"]
        conn.execute("DELETE FROM rotations WHERE m = ?", (m2_id,))

    # Mock raw_db and parse_any so backfill_match_rotations can load match2
    from app.db import db as raw_db
    monkeypatch.setattr(raw_db, "get_payload", lambda mid: {"raw": "dummy"} if mid == "match-waylay" else None)
    import app.backfill_rotations as bfr
    monkeypatch.setattr(bfr, "parse_any", lambda payload: match2)

    # Register Tester in tracked_players so Tester#NA1 resolves to puuid p1
    with db.connect() as conn:
        p1_pid = db._dim_id(conn, "player", "p1")
        conn.execute(
            "INSERT INTO tracked_players (puuid, name, tag, pid, requested_at) VALUES (?, ?, ?, ?, ?)",
            ("p1", "Tester", "NA1", p1_pid, 1700000000),
        )

    # Now query rotations for Tester#NA1
    # This should trigger backfill of match-waylay and return both Killjoy and Waylay in available_agents
    res = engine.rotations("Ascent", side="defense", player="Tester#NA1")
    assert "Killjoy" in res["available_agents"]
    assert "Waylay" in res["available_agents"]
    assert res["total_transitions"] == 2

    # Query specifically for Waylay
    waylay_res = engine.rotations("Ascent", side="defense", player="Tester#NA1", agent="Waylay")
    assert waylay_res["total_transitions"] == 1
    assert waylay_res["transitions"][0]["from_zone"] == "A Site"
    assert waylay_res["transitions"][0]["to_zone"] == "Market"

    # Also test an agent present only in kills (e.g. Clove)
    with db.connect() as conn:
        clove_id = db._dim_id(conn, "agent", "Clove")
        map_id = db._dim_id(conn, "map", "Ascent")
        conn.execute(
            """INSERT INTO kills (m, map_id, round_num, t_ms, side, ka_id, dmg_type, vx, vy, flags, killer_pid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m2_id, map_id, 1, 5000, 2, clove_id, 0, 5000, 5000, 0, p1_pid),
        )

    res_with_clove = engine.rotations("Ascent", side="defense", player="Tester#NA1")
    assert "Clove" in res_with_clove["available_agents"]

