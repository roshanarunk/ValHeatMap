"""Tests for the crawler's tracked-player handling."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.analytics_db import AnalyticsDB
from app.crawler import Crawler


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.headers: dict[str, str] = {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _RecordingClient:
    """Captures the URLs a crawl requests, answering 500 off-region.

    Mirrors the upstream behaviour that made this a bug: the matchlist
    endpoint is per-region and answers 500 -- not 404 -- for a player who
    is not on the region asked for.
    """

    def __init__(self, correct_region: str) -> None:
        self.correct_region = correct_region
        self.urls: list[str] = []

    async def get(self, url: str, headers=None, params=None):
        self.urls.append(url)
        if f"/{self.correct_region}/" not in url:
            return _FakeResponse(500)
        return _FakeResponse(200, {"data": []})


def test_tracked_player_is_crawled_on_their_own_region(tmp_path: Path):
    """A player's region comes from their account, not the crawler's default.

    Before this, a registered player outside the configured region got a
    500 on every request, stored nothing, and was marked crawled -- which
    looked exactly like someone with no matches.
    """
    db = AnalyticsDB(tmp_path / "a.db")
    db.track_player("puuid-ap", "Someone", "AP1", region="ap")

    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)
    client = _RecordingClient(correct_region="ap")
    asyncio.run(crawler.crawl_tracked_players(client, limit=1, per_player=3))

    assert client.urls, "the crawler should have made a request"
    assert all("/ap/" in u for u in client.urls), (
        f"expected the player's own region, got {client.urls}"
    )


def test_tracked_player_failure_does_not_wedge_the_queue(tmp_path: Path):
    """A player who always fails must not be retried ahead of everyone else."""
    db = AnalyticsDB(tmp_path / "b.db")
    db.track_player("puuid-bad", "Broken", "X", region="na")

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("upstream on fire")

    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)
    asyncio.run(crawler.crawl_tracked_players(_Boom(), limit=1))

    record = db.tracked_by_puuid("puuid-bad")
    assert record["crawled_at"] is not None, "a failed crawl must still be marked"


def test_players_needing_crawl_puts_new_registrations_first(tmp_path: Path):
    """Someone who just registered is watching an empty page."""
    import time

    db = AnalyticsDB(tmp_path / "c.db")
    db.track_player("old", "Old", "1", region="na")
    db.mark_player_crawled("old", 10)
    db.track_player("new", "New", "2", region="na")

    queue = db.players_needing_crawl(limit=5)
    assert queue[0]["puuid"] == "new", "never-crawled players come first"


class _HistoryClient:
    """Serves a paginated stored-matches listing, then one payload each.

    Mirrors the two-step shape of the real deep-history fetch: pages of
    summaries, then a full payload per match id we do not already have.
    Pagination is real here, because the crawler stops when a page repeats
    what it has already seen -- a stub that ignores `page` would look like
    an endpoint that does not paginate.
    """

    def __init__(
        self, ids: list[str], mode: str = "Competitive", page_size: int = 100
    ) -> None:
        self.ids = ids
        self.mode = mode
        self.page_size = page_size
        self.listing_calls = 0
        self.match_calls: list[str] = []

    async def get(self, url: str, headers=None, params=None):
        if "stored-matches" in url:
            self.listing_calls += 1
            page = int((params or {}).get("page", 1))
            size = int((params or {}).get("size", self.page_size))
            chunk = self.ids[(page - 1) * size : page * size]
            return _FakeResponse(
                200,
                {
                    "data": [
                        {"meta": {"id": i, "mode": self.mode, "map": {"name": "Ascent"}}}
                        for i in chunk
                    ]
                },
            )
        match_id = url.rsplit("/", 1)[-1]
        self.match_calls.append(match_id)
        return _FakeResponse(200, {"data": {"match_id": match_id}})


def test_history_fetches_every_match_in_the_listing(tmp_path: Path):
    """The v4 matchlist caps at 10 however large `size` is.

    That ceiling is fine for discovery, which only needs a few games per
    player to keep snowballing, but it was the whole history for someone
    looking at their own stats -- measured at 11 matches for a real
    account. stored-matches goes back ~100.
    """
    db = AnalyticsDB(tmp_path / "a.db")
    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)
    stored: list[str] = []
    crawler._store = lambda raw: (stored.append(raw["match_id"]), True)[1]

    client = _HistoryClient([f"m{i}" for i in range(40)])
    got = asyncio.run(crawler.crawl_player_history(client, "puuid", region="na"))

    # One full page then an empty one, which is how it learns it is done.
    assert client.listing_calls == 2
    assert got == 40
    assert len(stored) == 40


def test_history_walks_every_page(tmp_path: Path):
    """Deep history paginates: 366 matches over four pages for a real account.

    A single page would cap a tracked player's history at 100 matches,
    which is most of the reason this exists.
    """
    db = AnalyticsDB(tmp_path / "pages.db")
    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)
    crawler._store = lambda raw: True

    client = _HistoryClient([f"m{i}" for i in range(250)], page_size=100)
    got = asyncio.run(crawler.crawl_player_history(client, "puuid", region="na"))

    assert got == 250, "every page should be walked, not just the first"
    assert client.listing_calls == 4  # 100 + 100 + 50 + empty


def test_history_stops_if_the_endpoint_ignores_pagination(tmp_path: Path):
    """A listing that repeats itself must not loop until max_pages.

    Guards against burning a few hundred requests re-listing the same
    rows if the upstream ever drops `page` support.
    """
    db = AnalyticsDB(tmp_path / "loop.db")
    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)
    crawler._store = lambda raw: True

    class _NoPagination(_HistoryClient):
        async def get(self, url, headers=None, params=None):
            if "stored-matches" in url and params:
                params = {**params, "page": 1}  # always serve page 1
            return await super().get(url, headers, params)

    client = _NoPagination([f"m{i}" for i in range(20)])
    got = asyncio.run(crawler.crawl_player_history(client, "puuid", region="na"))

    assert got == 20
    assert client.listing_calls == 2, "should stop once a page adds nothing new"


def test_history_skips_matches_already_stored(tmp_path: Path):
    """Re-crawling a tracked player must not re-fetch their whole history.

    Skipping before the per-match request is what keeps a repeat crawl
    cheap -- otherwise every top-up costs ~100 requests.
    """
    db = AnalyticsDB(tmp_path / "b.db")
    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)
    crawler._store = lambda raw: True
    # Pretend the first three are already in the index.
    known = {"m0", "m1", "m2"}
    crawler.db.has_match = lambda mid: mid in known

    client = _HistoryClient(["m0", "m1", "m2", "m3", "m4"])
    got = asyncio.run(crawler.crawl_player_history(client, "puuid", region="na"))

    assert client.match_calls == ["m3", "m4"], "known matches must not be fetched"
    assert got == 2


def test_history_filters_by_mode(tmp_path: Path):
    """stored-matches returns every queue, including ones we do not want.

    Its mode names are display-cased ("Competitive"), unlike the lowercase
    slugs the v4 endpoint takes, so the comparison has to normalise.
    """
    db = AnalyticsDB(tmp_path / "c.db")
    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)
    crawler._store = lambda raw: True

    client = _HistoryClient(["dm1", "dm2"], mode="Deathmatch")
    got = asyncio.run(crawler.crawl_player_history(client, "puuid", region="na"))

    assert got == 0
    assert client.match_calls == [], "deathmatch should not be fetched"


def test_first_crawl_goes_deep_then_tops_up(tmp_path: Path):
    """A backfill is worth ~100 requests once; a top-up is not."""
    db = AnalyticsDB(tmp_path / "d.db")
    db.track_player("p1", "Someone", "NA1", region="na")
    crawler = Crawler(api_key="k", analytics=db, region="na", rate_limit=100_000)

    calls: list[str] = []

    async def _history(client, puuid, region=None, size=100):
        calls.append("history")
        return 40

    async def _recent(client, puuid, size=5, region=None):
        calls.append("recent")
        return 2

    crawler.crawl_player_history = _history
    crawler.crawl_player = _recent

    asyncio.run(crawler.crawl_tracked_players(object(), limit=1))
    assert calls == ["history"], "a never-crawled player gets the deep fetch"

    # They are now fresh, so the queue correctly leaves them alone. Age
    # the record past the freshness window to get the next crawl.
    with db.connect() as conn:
        conn.execute("UPDATE tracked_players SET crawled_at = 1 WHERE puuid = 'p1'")

    asyncio.run(crawler.crawl_tracked_players(object(), limit=1))
    assert calls == ["history", "recent"], "an already-crawled player only tops up"


def test_match_count_accumulates_across_crawls(tmp_path: Path):
    """A top-up of two must not make an 80-match player look like two."""
    db = AnalyticsDB(tmp_path / "e.db")
    db.track_player("p2", "Someone", "NA1", region="na")
    db.mark_player_crawled("p2", 80)
    db.mark_player_crawled("p2", 2)
    assert db.tracked_by_puuid("p2")["match_count"] == 82
