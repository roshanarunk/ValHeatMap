"""Unit tests for Pre-Match Scouting analytics, endpoints, and rotation agent attribution."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.analytics.scout import (
    _derive_counter_tips,
    _derive_tactical_tags,
    generate_lobby_summary,
    scout_player,
)
from app.analytics_db import AnalyticsDB
from app.main import app
from app.queries import QueryEngine


def test_tactical_tags_derivation():
    # Profile 1: Opening Threat + Clutch Threat
    s1 = {
        "opening_duels": 8,
        "first_bloods": 6,
        "first_deaths": 2,
        "opening_win_rate": 0.75,
        "clutches_won": 2,
        "clutch_win_rate": 0.40,
        "kills": 20,
        "deaths": 10,
        "support_rate": 0.50,
        "advantage_throw_rate": 0.10,
        "advantage_rounds_thrown": 1,
        "impact_kill_rate": 0.90,
    }
    tags1 = _derive_tactical_tags(s1, matches=2)
    tag_names1 = [t["tag"] for t in tags1]
    assert "🎯 Opening Duel Threat" in tag_names1
    assert "🏆 Clutch Specialist" in tag_names1
    assert "🔥 High Round Impact" in tag_names1
    assert "🤝 Disciplined Spacing" in tag_names1

    # Profile 2: Opening Liability + Advantage Thrower
    s2 = {
        "opening_duels": 6,
        "first_bloods": 1,
        "first_deaths": 5,
        "opening_win_rate": 0.166,
        "clutches_won": 0,
        "clutch_win_rate": 0.0,
        "kills": 8,
        "deaths": 14,
        "support_rate": 0.20,
        "advantage_throw_rate": 0.50,
        "advantage_rounds_thrown": 2,
        "impact_kill_rate": 0.50,
    }
    tags2 = _derive_tactical_tags(s2, matches=1)
    tag_names2 = [t["tag"] for t in tags2]
    assert "⚠️ Opening Liability" in tag_names2
    assert "🛑 Man-Advantage Overpeeker" in tag_names2
    assert "👤 Isolated Anchor / Lurker" in tag_names2


def test_counter_tips_generation():
    tags = [
        {"tag": "🎯 Opening Duel Threat", "type": "threat", "desc": ""},
        {"tag": "🛑 Man-Advantage Overpeeker", "type": "weakness", "desc": ""},
    ]
    def_rots = [
        {"from_zone": "B Main", "to_zone": "B Site", "count": 5, "win_rate": 0.80, "avg_duration_s": 12.0}
    ]
    tips = _derive_counter_tips({}, tags, def_rots, [], "Ascent")
    assert any("opening angles" in tip or "opening duels" in tip for tip in tips)
    assert any("4v5 or 3v4" in tip or "down a player" in tip for tip in tips)
    assert any("B Main ➔ B Site" in tip for tip in tips)


def test_lobby_summary_generation():
    reports = [
        {
            "riot_id": "Ace#NA1",
            "agent": "Jett",
            "agent_icon": "icon.png",
            "has_data": True,
            "kd": 1.60,
            "opening_duels": 5,
            "opening_win_rate": 0.80,
            "trade_rate": 0.30,
            "advantage_throw_rate": 0.10,
        },
        {
            "riot_id": "Bot#NA1",
            "agent": "Cypher",
            "agent_icon": "icon.png",
            "has_data": True,
            "kd": 0.70,
            "opening_duels": 4,
            "opening_win_rate": 0.25,
            "trade_rate": 0.10,
            "advantage_throw_rate": 0.40,
        },
    ]
    summary = generate_lobby_summary(reports)
    assert summary["top_threat"] is not None
    assert summary["top_threat"]["riot_id"] == "Ace#NA1"
    assert summary["weak_link"] is not None
    assert summary["weak_link"]["riot_id"] == "Bot#NA1"
    assert len(summary["playstyle_notes"]) >= 2


def test_scout_endpoints(monkeypatch: pytest.MonkeyPatch):
    async def _mock_henrik(name: str, tag: str) -> dict:
        return {}

    monkeypatch.setattr("app.main.clients.henrik_account", _mock_henrik)
    client = TestClient(app)

    # 1. Single player scout
    resp = client.get("/api/scout/player?riot_id=UnknownPlayer%239999&map_name=Ascent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["riot_id"] == "UnknownPlayer#9999"
    assert data["has_data"] is False
    assert "found" in data

    # 2. Lobby scout
    payload = {
        "map_name": "Ascent",
        "opponents": [
            {"riot_id": "UnknownA#0001", "agent": "Jett"},
            {"riot_id": "UnknownB#0002", "agent": "Omen"},
        ],
    }
    resp2 = client.post("/api/scout", json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["map_name"] == "Ascent"
    assert "lobby_summary" in data2
    assert len(data2["reports"]) == 2


def test_rotation_transition_agents():
    client = TestClient(app)
    resp = client.get("/api/rotations?map_name=Ascent&side=defense")
    assert resp.status_code == 200
    data = resp.json()
    assert "transitions" in data
    for t in data["transitions"]:
        assert "agents" in t
        assert isinstance(t["agents"], list)
