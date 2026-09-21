"""Tests for persistence and the crawler.

Everything here runs against a temporary database and fake HTTP responses --
no network, no API key, no touching the real dataset.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.crawler import Crawler, RateLimiter
from app.db import Database
from app.store import MatchStore


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Database:
    return Database(path=tmp_path / "test.db", raw_dir=tmp_path / "raw")


def _match_payload(match_id: str, map_name: str = "Ascent") -> dict:
    """A minimal HenrikDev v4 match with one plotted kill and one plant."""
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
                "puuid": "p-atk", "name": "Atk", "tag": "1", "team_id": "red",
                "agent": {"id": "", "name": "Jett"},
                "stats": {"kills": 1, "deaths": 0, "assists": 0, "score": 1,
                          "headshots": 0, "bodyshots": 1, "legshots": 0,
                          "damage": {"dealt": 150}},
            },
            {
                "puuid": "p-def", "name": "Def", "tag": "2", "team_id": "blue",
                "agent": {"id": "", "name": "Sage"},
                "stats": {"kills": 0, "deaths": 1, "assists": 0, "score": 0,
                          "headshots": 0, "bodyshots": 0, "legshots": 0,
                          "damage": {"dealt": 0}},
            },
        ],
        "teams": [
            {"team_id": "red", "won": True, "rounds": {"won": 13, "lost": 2}},
            {"team_id": "blue", "won": False, "rounds": {"won": 2, "lost": 13}},
        ],
        "kills": [
            {
                "round": 0, "time_in_round_in_ms": 20000, "time_in_match_in_ms": 20000,
                "killer": ref("p-atk", "red"), "victim": ref("p-def", "blue"),
                "assistants": [], "location": {"x": 1000, "y": 2000},
                "weapon": {"id": "w", "name": "Vandal", "type": "Weapon"},
                "secondary_fire_mode": False,
                "player_locations": [
                    {"player": ref("p-atk", "red"), "view_radians": 0.0,
                     "location": {"x": 1200, "y": 2200}},
                ],
            },
        ],
        "rounds": [
            {
                "id": 0, "result": "Detonate", "ceremony": "", "winning_team": "red",
                "plant": {
                    "round_time_in_ms": 40000, "site": "A",
                    "location": {"x": 900, "y": 1800},
                    "player": ref("p-atk", "red"), "player_locations": [],
                },
                "defuse": None, "stats": [],
            },
        ],
    }


# --- database -----------------------------------------------------------
def test_saves_and_indexes_a_match(tmp_db: Database):
    payload = _match_payload("m-1")
    path = tmp_db.save_match("m-1", payload, {"map_name": "Ascent", "mode": "standard", "kills": 1})
    assert path.exists()
    assert tmp_db.has_match("m-1")
    assert tmp_db.match_count() == 1
    # The raw payload round-trips unchanged: it is the source of truth.
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_saving_twice_does_not_duplicate(tmp_db: Database):
    for _ in range(3):
        tmp_db.save_match("m-1", _match_payload("m-1"), {"map_name": "Ascent"})
    assert tmp_db.match_count() == 1


def test_player_frontier_orders_by_tier(tmp_db: Database):
    tmp_db.add_player("low", "Low", "1", "na", tier=10)
    tmp_db.add_player("high", "High", "2", "na", tier=27)
    pending = tmp_db.pending_players(limit=5)
    assert [p["puuid"] for p in pending] == ["high", "low"]

    tmp_db.mark_player_crawled("high", 4)
    assert [p["puuid"] for p in tmp_db.pending_players(limit=5)] == ["low"]


def test_player_upsert_keeps_best_known_values(tmp_db: Database):
    tmp_db.add_player("p", "Name", "TAG", "na", tier=20)
    # A later sighting with no name must not blank the one we have.
    tmp_db.add_player("p", "", "", "na", tier=25)
    row = tmp_db.pending_players(limit=1)[0]
    assert row["name"] == "Name"
    assert row["tier"] == 25


def test_queue_skips_matches_already_stored(tmp_db: Database):
    assert tmp_db.enqueue_match("new-1") is True
    assert tmp_db.enqueue_match("new-1") is False  # already queued
    tmp_db.save_match("have-1", _match_payload("have-1"), {"map_name": "Ascent"})
    assert tmp_db.enqueue_match("have-1") is False  # already stored


def test_stats_reports_totals(tmp_db: Database):
    tmp_db.save_match("m-1", _match_payload("m-1"), {"map_name": "Ascent", "kills": 10, "plants": 2})
    tmp_db.save_match("m-2", _match_payload("m-2", "Bind"), {"map_name": "Bind", "kills": 5, "plants": 1})
    stats = tmp_db.stats()
    assert stats["matches"] == 2
    assert stats["kills"] == 15
    assert {r["map_name"] for r in stats["by_map"]} == {"Ascent", "Bind"}


# --- crawler ------------------------------------------------------------
def test_crawler_stores_and_discovers_players(tmp_db: Database):
    crawler = Crawler(api_key="test", database=tmp_db, verbose=False)
    assert crawler._store(_match_payload("m-1")) is True
    assert crawler.stored == 1
    assert tmp_db.has_match("m-1")
    # Both players in the match join the crawl frontier.
    assert {p["puuid"] for p in tmp_db.pending_players(limit=10)} == {"p-atk", "p-def"}


def test_crawler_skips_duplicates(tmp_db: Database):
    crawler = Crawler(api_key="test", database=tmp_db, verbose=False)
    crawler._store(_match_payload("m-1"))
    assert crawler._store(_match_payload("m-1")) is False
    assert crawler.stored == 1
    assert crawler.skipped == 1


def test_crawler_rejects_match_without_coordinates(tmp_db: Database):
    """A match with no kill positions cannot feed a heatmap."""
    payload = _match_payload("m-nocoord")
    payload["kills"][0]["location"] = {"x": -49794, "y": -800}  # sentinel
    crawler = Crawler(api_key="test", database=tmp_db, verbose=False)
    assert crawler._store(payload) is False
    assert not tmp_db.has_match("m-nocoord")


def test_crawler_survives_a_malformed_payload(tmp_db: Database):
    crawler = Crawler(api_key="test", database=tmp_db, verbose=False)
    assert crawler._store({"garbage": True}) is False
    assert crawler.errors == 1
    assert tmp_db.match_count() == 0


# --- rate limiting ------------------------------------------------------
def test_limiter_reads_budget_from_headers():
    limiter = RateLimiter(limit=90, remaining=90)
    limiter.observe(httpx.Headers({
        "x-ratelimit-limit": "90",
        "x-ratelimit-remaining": "42",
        "x-ratelimit-reset": "30",
    }))
    assert limiter.limit == 90
    assert limiter.remaining == 42
    assert limiter.reset_in == 30.0


def test_limiter_ignores_missing_or_junk_headers():
    limiter = RateLimiter(limit=90, remaining=50)
    limiter.observe(httpx.Headers({}))
    assert limiter.remaining == 50  # unchanged
    limiter.observe(httpx.Headers({"x-ratelimit-remaining": "not-a-number"}))
    assert limiter.remaining == 50


def test_limiter_backs_off_when_budget_is_low(monkeypatch):
    """With almost nothing left, the limiter waits out the reset window."""
    import asyncio

    import app.crawler as crawler_mod

    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    monkeypatch.setattr(crawler_mod.asyncio, "sleep", fake_sleep)

    async def drive(limiter: RateLimiter) -> None:
        # `_last` starts at 0, which reads as "ages ago"; set it to now so
        # the limiter actually has to wait for the next slot.
        limiter._last = asyncio.get_event_loop().time()
        monkeypatch.setattr(
            crawler_mod.time, "monotonic", lambda: limiter._last
        )
        await limiter.wait()

    asyncio.run(drive(RateLimiter(limit=90, remaining=1, reset_in=5.0)))
    assert slept, "expected the limiter to sleep when the budget is spent"
    assert slept[0] >= 5.0

    slept.clear()
    asyncio.run(drive(RateLimiter(limit=90, remaining=90, reset_in=60.0)))
    # Healthy budget: only the even pacing interval, 60/90 = 0.67s.
    assert slept and slept[0] == pytest.approx(60 / 90, abs=0.01)


# --- store integration --------------------------------------------------
def test_store_loads_crawled_matches_alongside_bundled(tmp_db: Database, tmp_path: Path):
    """A crawled match must show up in the app after a reload."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "sample.json").write_text(
        json.dumps(_match_payload("bundled-1", "Split")), encoding="utf-8"
    )
    tmp_db.save_match("crawled-1", _match_payload("crawled-1", "Haven"), {"map_name": "Haven"})

    store = MatchStore(bundled)
    for path in sorted(bundled.glob("*.json")):
        store._load_file(path, "local")
    # Drive the temp database explicitly; load_local() would reach for the
    # real one and pull in the live dataset.
    for path in tmp_db.iter_payload_paths():
        store._load_file(path, "henrik")
    # Mark loaded so all() does not trigger a global load of the real data.
    store._loaded = True

    ids = {m.meta.match_id for m in store.all()}
    assert ids == {"bundled-1", "crawled-1"}
    assert {m.meta.map_name for m in store.all()} == {"Split", "Haven"}


def test_concurrent_writers_do_not_collide(tmp_db: Database):
    """Two crawlers saving the same match must not fight over a temp file.

    A shared temp filename makes the rename fail outright on Windows, which
    is how this surfaced: a second crawler crashed mid-run.
    """
    import threading

    payload = _match_payload("m-race")
    errors: list[Exception] = []

    def save() -> None:
        try:
            for _ in range(5):
                tmp_db.save_match("m-race", payload, {"map_name": "Ascent"})
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent save failed: {errors[0]!r}"
    assert tmp_db.match_count() == 1
    # No temp files left behind.
    assert not list(tmp_db.raw_dir.glob("*.tmp"))
    # And the payload is intact, not truncated by an interleaved write.
    stored = json.loads((tmp_db.raw_dir / "m-race.json").read_text(encoding="utf-8"))
    assert stored == payload


def test_log_survives_names_the_console_cannot_encode(tmp_db: Database, capsys, monkeypatch):
    """A Cyrillic player name must not abort a long crawl on a cp1252 console."""
    import io

    crawler = Crawler(api_key="test", database=tmp_db, verbose=True)

    class Cp1252Stream(io.StringIO):
        encoding = "cp1252"

        def write(self, s: str) -> int:
            s.encode("cp1252")  # raises UnicodeEncodeError, like a real console
            return super().write(s)

    monkeypatch.setattr("sys.stdout", Cp1252Stream())
    crawler.log("crawling Ъ / ツ")  # must not raise


# --- crawl control (tray pause/resume) ----------------------------------
def test_control_pause_resume_and_toggle():
    from app.crawler import CrawlControl

    c = CrawlControl()
    assert c.paused is False and c.stopping is False

    c.pause()
    assert c.paused is True and c.status == "paused"
    c.resume()
    assert c.paused is False and c.status == "running"

    assert c.toggle() is True    # now paused
    assert c.toggle() is False   # back to running


def test_control_stop_clears_pause():
    """Stopping while paused must not leave the loop waiting forever."""
    from app.crawler import CrawlControl

    c = CrawlControl()
    c.pause()
    c.stop()
    assert c.stopping is True
    assert c.paused is False


def test_paused_crawler_makes_no_requests(tmp_db: Database):
    """A pause has to take effect inside a batch, not just between them."""
    import asyncio

    from app.crawler import CrawlControl, Crawler

    control = CrawlControl()
    control.pause()
    crawler = Crawler(api_key="test", database=tmp_db, verbose=False, control=control)

    async def attempt() -> str:
        # _get should sit in wait_while_paused rather than issuing a request.
        task = asyncio.ensure_future(crawler._get(None, "https://example.invalid"))
        await asyncio.sleep(0.1)
        assert not task.done(), "request proceeded while paused"
        assert crawler.requests == 0
        control.stop()
        return "stopped" if await task is None else "ran"

    assert asyncio.run(attempt()) == "stopped"


def test_tray_icons_differ_per_status():
    """Each status needs its own glyph; a shared icon tells you nothing."""
    pytest.importorskip("pystray")
    from app.tray import make_icon

    seen = {}
    for status in ("running", "paused", "publishing", "idle", "retrying", "stopped"):
        img = make_icon(status)
        assert img.size == (64, 64)
        seen[status] = img.tobytes()
    # Paused and running must be visually distinct at a glance.
    assert seen["paused"] != seen["running"]
    assert len(set(seen.values())) >= 4


# --- upload quota guard -------------------------------------------------
def test_quota_allows_a_normal_snapshot():
    from app.quota import QuotaState, check_upload

    state = QuotaState(month="2026-09")
    assert check_upload(37_000_000, state) is state


def test_quota_blocks_an_implausibly_large_snapshot():
    """A multi-GB snapshot means the database is broken, not that we grew."""
    from app.quota import QuotaExceeded, QuotaState, check_upload

    with pytest.raises(QuotaExceeded, match="self-imposed cap"):
        check_upload(3_000_000_000, QuotaState(month="2026-09"))


def test_quota_blocks_rapid_republishing():
    """Catches a runaway loop, which is the realistic way to burn the tier."""
    import time

    from app.quota import QuotaExceeded, QuotaState, check_upload

    state = QuotaState(month="2026-09", last_upload_ts=time.time())
    with pytest.raises(QuotaExceeded, match="minimum gap"):
        check_upload(37_000_000, state)


def test_quota_blocks_when_monthly_budget_is_spent():
    from app.quota import MAX_WRITES_PER_MONTH, QuotaExceeded, QuotaState, check_upload

    state = QuotaState(month="2026-09", writes=MAX_WRITES_PER_MONTH)
    with pytest.raises(QuotaExceeded, match="already this month"):
        check_upload(37_000_000, state)


def test_quota_resets_on_a_new_month(tmp_path: Path):
    """Cloudflare bills per calendar month, so the counters follow."""
    from datetime import datetime, timezone

    from app.quota import QuotaState

    path = tmp_path / "q.json"
    QuotaState(month="2000-01", writes=9_999, bytes_uploaded=10**9).save(path)

    loaded = QuotaState.load(path)
    assert loaded.writes == 0
    assert loaded.month == datetime.now(timezone.utc).strftime("%Y-%m")


def test_quota_counters_persist_within_a_month(tmp_path: Path):
    from app.quota import QuotaState, record_upload

    path = tmp_path / "q.json"
    state = QuotaState.load(path)
    record_upload(37_000_000, state).save(path)

    reloaded = QuotaState.load(path)
    assert reloaded.writes == 1
    assert reloaded.bytes_uploaded == 37_000_000


# --- public URL handling ------------------------------------------------
def test_public_base_accepts_a_bare_hostname(monkeypatch):
    """Pasting the hostname out of the Cloudflare dashboard must work.

    Without a scheme urllib raises "unknown url type", which is a useless
    error for what is really a config typo.
    """
    from app.snapshot import public_base

    for raw, expected in [
        ("data.example.com", "https://data.example.com"),
        ("https://data.example.com", "https://data.example.com"),
        ("https://data.example.com/", "https://data.example.com"),
        ("  data.example.com  ", "https://data.example.com"),
        ("http://localhost:9000", "http://localhost:9000"),
    ]:
        monkeypatch.setenv("R2_PUBLIC_URL", raw)
        assert public_base() == expected


def test_public_base_empty_when_unset(monkeypatch):
    from app.snapshot import public_base

    monkeypatch.delenv("R2_PUBLIC_URL", raising=False)
    assert public_base() == ""


# --- snapshot robustness ------------------------------------------------
def test_consistent_copy_captures_uncommitted_wal(tmp_path: Path):
    """A byte copy of a live WAL database is corrupt; this must not be.

    Writes sit in the -wal file until a checkpoint, so copying only the
    main file yields pages referencing WAL content the copy lacks. The
    result opens, then fails with "database disk image is malformed" on the
    first real query -- which is exactly what shipped to production.
    """
    import shutil
    import sqlite3

    from app.analytics_db import AnalyticsDB
    from app.snapshot import consistent_copy

    source = tmp_path / "live.db"
    db = AnalyticsDB(source)
    with db.connect() as conn:
        conn.execute("INSERT INTO dim (kind, id, name) VALUES ('map', 1, 'Ascent')")
        for i in range(500):
            conn.execute(
                "INSERT INTO matches (match_id, map_id, mode, rounds) VALUES (?,?,?,?)",
                (f"m{i}", 1, "standard", 24),
            )
    assert (tmp_path / "live.db-wal").exists(), "expected WAL mode"

    good = tmp_path / "good.db"
    consistent_copy(source, good)
    conn = sqlite3.connect(good)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 500
    conn.close()

    # The naive copy misses the WAL entirely: here the schema itself is
    # still only in the WAL, so the table does not even exist.
    naive = tmp_path / "naive.db"
    shutil.copy(source, naive)
    conn = sqlite3.connect(f"file:{naive}?immutable=1", uri=True)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        assert rows < 500, "naive copy unexpectedly complete"
    except sqlite3.DatabaseError:
        pass  # equally valid: the copy is unusable
    finally:
        conn.close()


def test_snapshot_requests_carry_a_user_agent():
    """Cloudflare answers urllib's default UA with 403, even when public."""
    from app.snapshot import USER_AGENT, _request

    request = _request("https://example.com/x.gz")
    assert request.get_header("User-agent") == USER_AGENT
    assert "Python-urllib" not in USER_AGENT


def test_public_url_accepts_bare_host(monkeypatch):
    from app.snapshot import public_base

    monkeypatch.setenv("R2_PUBLIC_URL", "data.example.com")
    assert public_base() == "https://data.example.com"


def test_failed_download_yields_an_empty_but_valid_database(monkeypatch, tmp_path: Path):
    """An unreachable snapshot must not take every route down with it.

    Returning DEFAULT_PATH was worse than useless on a server, where that
    file does not exist: every request then failed with a SQLite error,
    including /api/health. An empty database lets the app start and report
    zero, which is diagnosable.
    """
    import sqlite3

    import app.snapshot as snap

    monkeypatch.setattr(snap, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(snap, "CACHED_DB", tmp_path / "cached.db")
    monkeypatch.setenv("VALHEATMAP_SNAPSHOT_URL", "https://nonexistent.invalid/x.db.gz")

    path = snap.ensure_local_db()
    assert path.exists()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
    finally:
        conn.close()


def test_etag_check_bypasses_the_cdn_cache(monkeypatch):
    """The freshness check must ask the origin, not the edge.

    A plain HEAD is served from the same CDN cache as the file, so for the
    length of the edge TTL after a publish it returns the *old* ETag. The
    poller then concludes nothing changed and sleeps out its whole
    interval, which is why a fresh publish appeared not to reach the site.
    """
    import app.snapshot as snap

    seen: dict[str, object] = {}

    class FakeResponse:
        headers = {"ETag": '"new"'}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["cache_control"] = request.get_header("Cache-control")
        return FakeResponse()

    monkeypatch.setenv("VALHEATMAP_SNAPSHOT_URL", "https://cdn.example/a.db.gz")
    monkeypatch.setattr(snap.urllib.request, "urlopen", fake_urlopen)

    assert snap.remote_etag() == '"new"'
    # Cache-busted query string and an explicit no-cache both matter: some
    # CDNs honour one and not the other.
    assert "?_=" in seen["url"] or "&_=" in seen["url"]
    assert seen["cache_control"] == "no-cache"


def test_forced_download_also_bypasses_the_cache(monkeypatch, tmp_path: Path):
    """Otherwise the edge hands back the copy we just rejected."""
    import app.snapshot as snap

    seen: dict[str, object] = {}

    def fake_download(request, tmp, url):
        seen["url"] = request.full_url
        seen["cache_control"] = request.get_header("Cache-control")
        return tmp_path / "db"

    monkeypatch.setenv("VALHEATMAP_SNAPSHOT_URL", "https://cdn.example/a.db.gz")
    monkeypatch.setattr(snap, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(snap, "CACHED_DB", tmp_path / "cached.db")
    monkeypatch.setattr(snap, "_download", fake_download)

    snap.ensure_local_db(force=True)
    assert "_=" in seen["url"]
    assert seen["cache_control"] == "no-cache"


def test_publish_refuses_a_database_too_large_for_the_function(tmp_path: Path):
    """The failure this prevents is silent and total.

    An oversized upload does not error: the download fills the function's
    disk, the fallback kicks in, and every route 500s. Checking before the
    bytes leave is the only place it is cheap to catch.
    """
    import app.publish as pub

    big = tmp_path / "big.db"
    big.write_bytes(b"x" * 1024)

    original = pub.TMP_BUDGET_BYTES
    try:
        pub.TMP_BUDGET_BYTES = 1000  # so 1 KB is "too big"
        with pytest.raises(RuntimeError, match="safely fits"):
            pub.publish(db_path=big, verbose=False)
    finally:
        pub.TMP_BUDGET_BYTES = original
