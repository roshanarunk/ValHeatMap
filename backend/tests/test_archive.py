"""Tests for parted zip archiving of raw match payloads."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.analytics_db import AnalyticsDB
from app.archive_raw import main as archive_cli_main
from app.backfill_players import _payload_for
from app.build_analytics import build as build_analytics
from app.db import Database


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Database:
    return Database(path=tmp_path / "test.db", raw_dir=tmp_path / "raw")


def _sample_payload(match_id: str, map_name: str = "Ascent") -> dict:
    def ref(puuid: str, team: str) -> dict:
        return {"puuid": puuid, "name": puuid, "tag": "1", "team": team}

    return {
        "metadata": {
            "match_id": match_id,
            "map": {"id": "m", "name": map_name},
            "game_length_in_ms": 1000,
            "started_at": "2024-05-01T10:00:00Z",
            "queue": {"id": "competitive", "name": "Competitive", "mode_type": "Standard"},
            "region": "na",
        },
        "players": [
            {
                "puuid": "p-atk",
                "name": "Atk",
                "tag": "1",
                "team_id": "red",
                "agent": {"id": "", "name": "Jett"},
                "tier": {"id": 24, "name": "Immortal 1"},
                "stats": {"kills": 1, "deaths": 0, "assists": 0, "score": 1, "damage": {"dealt": 150}},
            },
            {
                "puuid": "p-def",
                "name": "Def",
                "tag": "2",
                "team_id": "blue",
                "agent": {"id": "", "name": "Sage"},
                "tier": {"id": 24, "name": "Immortal 1"},
                "stats": {"kills": 0, "deaths": 1, "assists": 0, "score": 0, "damage": {"dealt": 0}},
            },
        ],
        "teams": [
            {"team_id": "red", "won": True, "rounds": {"won": 13, "lost": 2}},
            {"team_id": "blue", "won": False, "rounds": {"won": 2, "lost": 13}},
        ],
        "kills": [
            {
                "round": 0,
                "time_in_round_in_ms": 20000,
                "time_in_match_in_ms": 20000,
                "killer": ref("p-atk", "red"),
                "victim": ref("p-def", "blue"),
                "assistants": [],
                "location": {"x": 1000, "y": 2000},
                "weapon": {"id": "w", "name": "Vandal", "type": "Weapon"},
                "secondary_fire_mode": False,
                "player_locations": [
                    {"player": ref("p-atk", "red"), "view_radians": 0.0, "location": {"x": 1200, "y": 2200}},
                ],
            }
        ],
        "rounds": [
            {
                "id": 0,
                "result": "Detonate",
                "ceremony": "",
                "winning_team": "red",
                "plant": {
                    "round_time_in_ms": 40000,
                    "site": "A",
                    "location": {"x": 900, "y": 1800},
                    "player": ref("p-atk", "red"),
                    "player_locations": [],
                },
                "defuse": None,
                "stats": [],
            }
        ],
    }


def test_archive_splits_into_numbered_zip_parts(tmp_db: Database):
    """5 matches with chunk_size=2 should produce 3 distinct zip parts."""
    for i in range(1, 6):
        mid = f"match-{i}"
        payload = _sample_payload(mid, map_name="Haven" if i % 2 == 0 else "Ascent")
        tmp_db.save_match(mid, payload, {"map_name": payload["metadata"]["map"]["name"], "started_at": i * 1000})

    # Initially 5 loose files exist
    assert len(list(tmp_db.raw_dir.glob("*.json"))) == 5

    res = tmp_db.archive_raw(chunk_size=2)
    assert res["archived"] == 5
    assert res["archives_created"] == 3
    assert res["bytes_freed"] > 0

    # Loose JSON files were deleted
    assert len(list(tmp_db.raw_dir.glob("*.json"))) == 0

    # 3 sequence-numbered zip parts exist
    zip_files = sorted(tmp_db.raw_dir.glob("archive_*.zip"))
    assert len(zip_files) == 3
    assert zip_files[0].name == "archive_0001.zip"
    assert zip_files[1].name == "archive_0002.zip"
    assert zip_files[2].name == "archive_0003.zip"

    # Verify part 1 contains 2 matches, part 2 has 2, part 3 has 1
    with zipfile.ZipFile(zip_files[0]) as z1:
        assert len(z1.namelist()) == 2
        assert "match-1.json" in z1.namelist()
        assert "match-2.json" in z1.namelist()

    with zipfile.ZipFile(zip_files[1]) as z2:
        assert len(z2.namelist()) == 2
        assert "match-3.json" in z2.namelist()
        assert "match-4.json" in z2.namelist()

    with zipfile.ZipFile(zip_files[2]) as z3:
        assert len(z3.namelist()) == 1
        assert "match-5.json" in z3.namelist()


def test_individual_zip_part_can_be_extracted_independently(tmp_db: Database, tmp_path: Path):
    """Any zip part can be unzipped independently on its own."""
    for i in range(1, 4):
        mid = f"match-{i}"
        tmp_db.save_match(mid, _sample_payload(mid), {"map_name": "Ascent", "started_at": i})

    tmp_db.archive_raw(chunk_size=2)
    zip_part = tmp_db.raw_dir / "archive_0001.zip"
    assert zip_part.exists()

    extract_dir = tmp_path / "extracted_part1"
    extract_dir.mkdir()

    # Independent unzip
    with zipfile.ZipFile(zip_part) as zf:
        zf.extractall(extract_dir)

    extracted_files = sorted(p.name for p in extract_dir.glob("*.json"))
    assert extracted_files == ["match-1.json", "match-2.json"]


def test_get_payload_transparently_reads_from_zip(tmp_db: Database):
    """db.get_payload reads O(1) from zip archive without extracting to disk."""
    for i in range(1, 4):
        mid = f"match-{i}"
        tmp_db.save_match(mid, _sample_payload(mid, map_name=f"Map{i}"), {"map_name": f"Map{i}", "started_at": i})

    tmp_db.archive_raw(chunk_size=10)

    # All files archived
    assert len(list(tmp_db.raw_dir.glob("*.json"))) == 0

    p1 = tmp_db.get_payload("match-1")
    assert p1 is not None
    assert p1["metadata"]["match_id"] == "match-1"
    assert p1["metadata"]["map"]["name"] == "Map1"

    p2 = tmp_db.get_payload("match-2")
    assert p2 is not None
    assert p2["metadata"]["match_id"] == "match-2"
    assert p2["metadata"]["map"]["name"] == "Map2"

    assert tmp_db.get_payload("nonexistent") is None


def test_iter_payloads_yields_all_matches_in_order(tmp_db: Database):
    """iter_payloads yields every match payload efficiently."""
    for i in range(1, 6):
        mid = f"match-{i}"
        tmp_db.save_match(mid, _sample_payload(mid), {"map_name": "Ascent", "started_at": i * 100})

    # Archive first 4, leave 5th loose
    tmp_db.archive_raw(chunk_size=2, max_chunks=2)

    # Save a 6th loose match
    tmp_db.save_match("match-6", _sample_payload("match-6"), {"map_name": "Ascent", "started_at": 600})

    results = list(tmp_db.iter_payloads())
    match_ids = [mid for mid, _ in results]
    # ORDER BY started_at DESC
    assert match_ids == ["match-6", "match-5", "match-4", "match-3", "match-2", "match-1"]
    for mid, payload in results:
        assert payload["metadata"]["match_id"] == mid


def test_build_analytics_runs_over_archived_zip_payloads(tmp_db: Database, tmp_path: Path):
    """build_analytics successfully populates AnalyticsDB directly from zip parts."""
    adb = AnalyticsDB(path=tmp_path / "analytics_test.db")

    for i in range(1, 5):
        mid = f"m-{i}"
        tmp_db.save_match(mid, _sample_payload(mid), {"map_name": "Ascent", "started_at": i})

    tmp_db.archive_raw(chunk_size=2)

    # Temporarily point default_db to tmp_db for build_analytics
    from app import build_analytics as ba_module
    orig_db = ba_module.raw_db
    ba_module.raw_db = tmp_db
    try:
        res = build_analytics(adb, rebuild=True, verbose=False)
        assert res["stored"] == 4
        assert res["failed"] == 0
        stats = adb.stats()
        assert stats["matches"] == 4
        assert stats["kills"] == 4
    finally:
        ba_module.raw_db = orig_db


def test_backfill_reads_from_zip_archives(tmp_db: Database, monkeypatch):
    """_payload_for reads from zipped parts transparently."""
    for i in range(1, 3):
        mid = f"bf-{i}"
        tmp_db.save_match(mid, _sample_payload(mid), {"map_name": "Ascent", "started_at": i})

    tmp_db.archive_raw(chunk_size=5)

    import app.backfill_players as bf_mod
    monkeypatch.setattr(bf_mod, "raw_db", tmp_db)

    payload = _payload_for("bf-1")
    assert payload is not None
    assert payload["metadata"]["match_id"] == "bf-1"


def test_subsequent_archives_increment_sequence(tmp_db: Database):
    """New archives pick up where the last archive index left off."""
    for i in range(1, 3):
        tmp_db.save_match(f"m-{i}", _sample_payload(f"m-{i}"), {"map_name": "Ascent", "started_at": i})
    tmp_db.archive_raw(chunk_size=2)

    assert (tmp_db.raw_dir / "archive_0001.zip").exists()

    # Add 2 more matches
    for i in range(3, 5):
        tmp_db.save_match(f"m-{i}", _sample_payload(f"m-{i}"), {"map_name": "Ascent", "started_at": i})
    tmp_db.archive_raw(chunk_size=2)

    assert (tmp_db.raw_dir / "archive_0002.zip").exists()
