"""Tests for the Fly seed's carry-over merge.

The seed replaces the whole database file. Without a merge it silently
discards whatever the remote crawler collected while the upload was being
prepared, which after a few days is thousands of matches.

This exercises the same SQL the install script runs, so a change there
that breaks re-keying is caught here rather than on the volume.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.analytics.kills import enrich
from app.analytics_db import AnalyticsDB
from app.reference import get_map

from test_queries import _match


def _carry_over(seed_path: Path, live_path: Path) -> int:
    """The merge from deploy/seed-fly.ps1, kept in step with it."""
    conn = sqlite3.connect(seed_path)
    conn.execute("ATTACH ? AS live", (str(live_path),))
    missing = [
        r[0]
        for r in conn.execute(
            "SELECT match_id FROM live.matches"
            " WHERE match_id NOT IN (SELECT match_id FROM main.matches)"
        )
    ]
    for match_id in missing:
        old_id = conn.execute(
            "SELECT id FROM live.matches WHERE match_id = ?", (match_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO main.matches"
            " (match_id, map_id, mode, queue, act_id, patch_id, region,"
            "  started_at, avg_tier, rounds)"
            " SELECT match_id, map_id, mode, queue, act_id, patch_id, region,"
            "        started_at, avg_tier, rounds FROM live.matches WHERE id = ?",
            (old_id,),
        )
        new_id = cur.lastrowid
        conn.execute(
            "INSERT INTO main.kills"
            " (m, map_id, act_id, avg_tier, round_num, t_ms, side, ka_id, va_id,"
            "  weapon_id, ability_id, dmg_type, vx, vy, kx, ky, flags)"
            " SELECT ?, map_id, act_id, avg_tier, round_num, t_ms, side, ka_id, va_id,"
            "        weapon_id, ability_id, dmg_type, vx, vy, kx, ky, flags"
            " FROM live.kills WHERE m = ?",
            (new_id, old_id),
        )
        conn.execute(
            "INSERT INTO main.plants"
            " (m, map_id, act_id, avg_tier, round_num, t_ms, site, x, y, won, defused)"
            " SELECT ?, map_id, act_id, avg_tier, round_num, t_ms, site, x, y, won, defused"
            " FROM live.plants WHERE m = ?",
            (new_id, old_id),
        )
    conn.commit()
    conn.execute("DETACH live")
    conn.close()
    return len(missing)


def _build(path: Path, ids: list[str]) -> AnalyticsDB:
    db = AnalyticsDB(path)
    info = get_map("Ascent")
    for match_id in ids:
        m = _match(match_id)
        db.add_match(m, info, enrich(m))
    return db


def test_seed_carries_over_matches_the_remote_crawler_added(tmp_path: Path):
    seed = _build(tmp_path / "seed.db", ["a", "b"])
    _build(tmp_path / "live.db", ["a", "b", "c", "d"])

    carried = _carry_over(tmp_path / "seed.db", tmp_path / "live.db")
    assert carried == 2

    stats = AnalyticsDB(tmp_path / "seed.db").stats()
    assert stats["matches"] == 4
    assert stats["kills"] == 16      # 4 matches x 4 kills


def test_carried_matches_keep_their_own_kills(tmp_path: Path):
    """Row ids differ between the databases, so kills must be re-keyed.

    A mis-keyed merge shows up as a match with no kills, or one holding
    another match's -- both of which this catches.
    """
    _build(tmp_path / "seed.db", ["a"])
    _build(tmp_path / "live.db", ["a", "b", "c"])
    _carry_over(tmp_path / "seed.db", tmp_path / "live.db")

    conn = sqlite3.connect(tmp_path / "seed.db")
    rows = conn.execute(
        "SELECT m.match_id, COUNT(k.m) n FROM matches m"
        " LEFT JOIN kills k ON k.m = m.id GROUP BY m.id"
    ).fetchall()
    assert {r[0] for r in rows} == {"a", "b", "c"}
    assert all(r[1] == 4 for r in rows), f"kills mis-keyed: {rows}"


def test_carry_over_does_not_duplicate_shared_matches(tmp_path: Path):
    """Both sides hold most of the same matches; only the extras move."""
    _build(tmp_path / "seed.db", ["a", "b", "c"])
    _build(tmp_path / "live.db", ["a", "b", "c"])

    assert _carry_over(tmp_path / "seed.db", tmp_path / "live.db") == 0
    conn = sqlite3.connect(tmp_path / "seed.db")
    dupes = conn.execute(
        "SELECT match_id FROM matches GROUP BY match_id HAVING COUNT(*) > 1"
    ).fetchall()
    assert dupes == []


def test_seeded_attribution_survives_the_merge(tmp_path: Path):
    """The point of the seed is the attribution; the merge must not undo it."""
    _build(tmp_path / "seed.db", ["a", "b"])
    _build(tmp_path / "live.db", ["a", "b", "c"])
    _carry_over(tmp_path / "seed.db", tmp_path / "live.db")

    conn = sqlite3.connect(tmp_path / "seed.db")
    attributed = conn.execute(
        "SELECT COUNT(*) FROM kills WHERE killer_pid IS NOT NULL"
    ).fetchone()[0]
    assert attributed == 8, "the two seeded matches should keep their attribution"
