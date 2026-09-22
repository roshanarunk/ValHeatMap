"""Tests for app/load.py's busy-detection and quiet-hours logic.

Added after an incident where disk I/O contention (a plain COUNT(*)
against the `kills` table taking 4.8-13s) was invisible to the existing
load-average and free-memory checks. The quiet-hours boundary and the
query-latency probe are exactly the kind of off-by-one and
platform-availability logic that is cheap to get wrong and expensive to
discover wrong in production, so they get direct coverage here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import load

ET = ZoneInfo("America/New_York")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 22, hour, minute, tzinfo=ET)


def test_quiet_hours_covers_noon_through_eleven_pm():
    for hour in range(12, 24):
        assert load.in_quiet_hours(_at(hour)), f"{hour}:00 ET should be quiet hours"


def test_quiet_hours_excludes_midnight_through_eleven_am():
    for hour in range(0, 12):
        assert not load.in_quiet_hours(_at(hour)), f"{hour}:00 ET should not be quiet hours"


def test_quiet_hours_boundary_is_exact():
    """The two edges of the window, checked to the minute either side."""
    assert not load.in_quiet_hours(_at(11, 59))
    assert load.in_quiet_hours(_at(12, 0))
    assert load.in_quiet_hours(_at(23, 59))
    assert not load.in_quiet_hours(_at(0, 0))  # midnight is excluded


def test_quiet_hours_converts_from_other_timezones():
    """A naive assumption that `now` is already in the target zone would
    silently shift the window by whatever the input's offset is."""
    utc_afternoon = datetime(2026, 9, 22, 17, 0, tzinfo=ZoneInfo("UTC"))  # 1pm ET
    assert load.in_quiet_hours(utc_afternoon)
    utc_early = datetime(2026, 9, 22, 4, 0, tzinfo=ZoneInfo("UTC"))  # midnight ET
    assert not load.in_quiet_hours(utc_early)


def test_probe_latency_is_fast_against_a_small_table(tmp_path: Path):
    db_path = tmp_path / "probe.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE kills (map_id INTEGER)")
    conn.executemany("INSERT INTO kills VALUES (?)", [(1,)] * 100)
    conn.commit()
    conn.close()

    latency = load.probe_latency(str(db_path))
    assert latency is not None
    assert latency < 0.5, f"a 100-row table should probe fast, took {latency:.2f}s"


def test_probe_latency_returns_none_for_a_missing_database():
    """A probe failure must not be mistaken for a fast, healthy result --
    is_busy() relies on None meaning "couldn't tell", not "0 seconds"."""
    assert load.probe_latency("/nonexistent/path/does/not/exist.db") is None


def test_is_busy_uses_the_latency_probe_when_a_db_path_is_given(tmp_path: Path):
    db_path = tmp_path / "slow.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE kills (map_id INTEGER)")
    conn.commit()
    conn.close()

    # A latency_ceiling of 0 means any measurable time counts as busy --
    # simulates the contended-disk case without needing to actually
    # contend a real disk in a unit test.
    assert load.is_busy(db_path=str(db_path), latency_ceiling=0.0) is True


def test_is_busy_ignores_latency_when_no_db_path_given():
    """Callers that have not built the database path yet (or are checking
    before the crawler even knows it) must still get an answer from the
    other two signals, not an error."""
    result = load.is_busy(db_path=None)
    assert isinstance(result, bool)
