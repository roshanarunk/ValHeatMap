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

    crawler = Crawler(api_key="k", analytics=db, region="na")
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

    crawler = Crawler(api_key="k", analytics=db, region="na")
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
