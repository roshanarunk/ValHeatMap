"""Dataset builder: crawls HenrikDev for matches and stores them.

Strategy
--------
Snowball sampling. Seed from the ranked leaderboard (active, high-elo
players whose matches have the coordinated play that makes trade and
plant-win-rate stats meaningful), then:

  1. take an uncrawled player from the frontier
  2. fetch their recent matches -- the v4 matchlist returns *full* match
     objects, so one request yields several complete matches
  3. store each match, and add all 10 players in it to the frontier

Because every match contributes up to 10 new players, the frontier grows
faster than it drains; the practical limit is rate limit and disk, not
discovery.

Rate limiting is driven by the API's own `x-ratelimit-*` response headers
rather than a hardcoded rate, so the crawler adapts if the key's tier
changes. Run it with:

    python -m app.crawler --matches 500
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import load_env
from .db import Database, db as default_db
from .store import parse_any

HENRIK_BASE = "https://api.henrikdev.xyz/valorant"
# Queues worth collecting: these have rounds, plants and sides.
DEFAULT_MODES = ("competitive", "unrated")


@dataclass
class RateLimiter:
    """Self-pacing limiter driven by the API's rate-limit headers.

    Starts at the configured budget and corrects itself from the
    `x-ratelimit-remaining` / `-reset` headers on every response. When the
    remaining budget runs low it spreads the leftover requests across the
    window instead of sprinting into a 429.
    """

    limit: int = 90
    window_s: float = 60.0
    remaining: int = 90
    reset_in: float = 60.0
    _last: float = field(default=0.0, repr=False)

    async def wait(self) -> None:
        now = time.monotonic()
        # Base pace: spread `limit` requests evenly over the window.
        interval = self.window_s / max(1, self.limit)
        if self.remaining <= 2:
            # Nearly exhausted: hold until the window resets.
            delay = max(interval, self.reset_in)
        elif self.remaining < self.limit * 0.2:
            # Running low: stretch the remaining budget over the reset.
            delay = max(interval, self.reset_in / max(1, self.remaining))
        else:
            delay = interval
        elapsed = now - self._last
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last = time.monotonic()

    def observe(self, headers: httpx.Headers) -> None:
        def as_int(key: str) -> int | None:
            raw = headers.get(key)
            if raw is None:
                return None
            try:
                return int(float(raw))
            except ValueError:
                return None

        limit = as_int("x-ratelimit-limit")
        if limit:
            self.limit = limit
        remaining = as_int("x-ratelimit-remaining")
        if remaining is not None:
            self.remaining = remaining
        reset = as_int("x-ratelimit-reset")
        if reset is not None:
            self.reset_in = float(reset)


class Crawler:
    def __init__(
        self,
        api_key: str,
        database: Database | None = None,
        region: str = "na",
        modes: tuple[str, ...] = DEFAULT_MODES,
        rate_limit: int = 90,
        verbose: bool = True,
    ) -> None:
        self.key = api_key
        self.db = database or default_db
        self.region = region
        self.modes = modes
        self.limiter = RateLimiter(limit=rate_limit, remaining=rate_limit)
        self.verbose = verbose
        self.stored = 0
        self.skipped = 0
        self.errors = 0
        self.requests = 0

    def log(self, message: str) -> None:
        """Print progress without ever killing the crawl.

        Player names are arbitrary unicode, and a Windows console defaults
        to cp1252 -- a Cyrillic or Japanese name would otherwise raise
        UnicodeEncodeError and abort a long-running crawl over a log line.
        """
        if not self.verbose:
            return
        try:
            print(message, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            print(message.encode(encoding, errors="replace").decode(encoding), flush=True)

    async def _get(
        self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None
    ) -> Any | None:
        """One rate-limited GET, retrying once on 429 or a transient error."""
        for attempt in range(2):
            await self.limiter.wait()
            try:
                resp = await client.get(
                    url, headers={"Authorization": self.key}, params=params
                )
            except httpx.RequestError as exc:
                self.log(f"  ! network error: {exc}")
                self.errors += 1
                await asyncio.sleep(2)
                continue
            self.requests += 1
            self.limiter.observe(resp.headers)

            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after") or self.limiter.reset_in or 60)
                self.log(f"  · rate limited, waiting {wait:.0f}s")
                await asyncio.sleep(wait + 1)
                continue
            if resp.status_code in (404, 400):
                return None
            if resp.status_code >= 500:
                await asyncio.sleep(2)
                continue
            if resp.status_code >= 400:
                self.log(f"  ! HTTP {resp.status_code} for {url}")
                self.errors += 1
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        return None

    # --- seeding -------------------------------------------------------
    async def seed_from_leaderboard(self, client: httpx.AsyncClient, size: int = 100) -> int:
        """Populate the frontier with ranked players."""
        payload = await self._get(
            client, f"{HENRIK_BASE}/v3/leaderboard/{self.region}/pc", {"size": size}
        )
        if not payload:
            return 0
        players = (payload.get("data") or {}).get("players") or []
        added = 0
        for p in players:
            if p.get("is_anonymized") or p.get("is_banned"):
                continue
            puuid = p.get("puuid")
            if not puuid:
                continue
            self.db.add_player(
                puuid=puuid,
                name=p.get("name") or "",
                tag=p.get("tag") or "",
                region=self.region,
                tier=int(p.get("tier") or 0),
            )
            added += 1
        self.log(f"seeded {added} players from the {self.region.upper()} leaderboard")
        return added

    async def seed_from_riot_id(self, client: httpx.AsyncClient, name: str, tag: str) -> bool:
        payload = await self._get(client, f"{HENRIK_BASE}/v2/account/{name}/{tag}")
        data = (payload or {}).get("data") or {}
        puuid = data.get("puuid")
        if not puuid:
            self.log(f"  ! could not resolve {name}#{tag}")
            return False
        self.db.add_player(puuid, name, tag, data.get("region") or self.region, tier=0)
        self.log(f"seeded {name}#{tag}")
        return True

    # --- crawling ------------------------------------------------------
    def _store(self, raw: dict[str, Any]) -> bool:
        """Parse, validate and persist one match payload."""
        try:
            match = parse_any(raw, source="henrik")
        except (ValueError, KeyError, TypeError) as exc:
            self.log(f"  ! unparseable match: {exc}")
            self.errors += 1
            return False
        match_id = match.meta.match_id
        if not match_id:
            return False
        if self.db.has_match(match_id):
            self.skipped += 1
            return False
        # A match with no kill coordinates is useless for a heatmap.
        if not any(k.victim_location is not None for k in match.kills):
            self.skipped += 1
            return False

        summary = {
            "map_name": match.meta.map_name,
            "mode": match.meta.mode,
            "queue": match.meta.queue,
            "region": match.meta.region or self.region,
            "started_at": match.meta.started_at,
            "rounds": len(match.rounds),
            "kills": len(match.kills),
            "plants": len(match.plants),
            "source": "henrik",
        }
        self.db.save_match(match_id, raw, summary)
        self.db.mark_match(match_id, "done")
        self.stored += 1

        # Every player in this match is a new crawl candidate.
        for player in match.players:
            self.db.add_player(
                player.puuid, player.name, player.tag,
                match.meta.region or self.region, player.tier,
            )
        return True

    async def crawl_player(
        self, client: httpx.AsyncClient, puuid: str, size: int = 5
    ) -> int:
        """Fetch a player's recent matches. Returns how many were stored."""
        stored = 0
        for mode in self.modes:
            payload = await self._get(
                client,
                f"{HENRIK_BASE}/v4/by-puuid/matches/{self.region}/pc/{puuid}",
                {"size": size, "mode": mode},
            )
            for raw in (payload or {}).get("data") or []:
                if self._store(raw):
                    stored += 1
        self.db.mark_player_crawled(puuid, stored)
        return stored

    async def run(self, target_matches: int = 200, per_player: int = 5) -> dict[str, Any]:
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            if not self.db.pending_players(limit=1):
                await self.seed_from_leaderboard(client)

            while self.stored < target_matches:
                batch = self.db.pending_players(limit=10, region=self.region)
                if not batch:
                    # Frontier drained: top it up from the leaderboard.
                    if not await self.seed_from_leaderboard(client, size=200):
                        self.log("frontier empty and no new seeds; stopping")
                        break
                    continue

                for player in batch:
                    if self.stored >= target_matches:
                        break
                    got = await self.crawl_player(client, player["puuid"], per_player)
                    label = player.get("name") or player["puuid"][:8]
                    self.log(
                        f"[{self.stored:>5}/{target_matches}] {label:<20} +{got} "
                        f"(skip {self.skipped}, req {self.requests})"
                    )

        elapsed = time.monotonic() - started
        return {
            "stored": self.stored,
            "skipped": self.skipped,
            "errors": self.errors,
            "requests": self.requests,
            "elapsed_s": round(elapsed, 1),
            "rate_per_min": round(self.requests / (elapsed / 60), 1) if elapsed > 0 else 0,
        }


async def _main(args: argparse.Namespace) -> int:
    load_env()
    key = os.environ.get("HENRIK_API_KEY")
    if not key:
        print("HENRIK_API_KEY is not set (put it in .env or the environment).", file=sys.stderr)
        return 1

    crawler = Crawler(
        api_key=key,
        region=args.region,
        modes=tuple(args.modes.split(",")),
        rate_limit=int(os.environ.get("HENRIK_RATE_LIMIT", args.rate)),
    )
    if args.seed:
        name, _, tag = args.seed.partition("#")
        async with httpx.AsyncClient(timeout=30) as client:
            await crawler.seed_from_riot_id(client, name, tag)

    result = await crawler.run(target_matches=args.matches, per_player=args.per_player)
    print("\n--- crawl complete ---")
    for key_, value in result.items():
        print(f"{key_:>14}: {value}")
    stats = crawler.db.stats()
    print(f"{'total matches':>14}: {stats['matches']}")
    print(f"{'total kills':>14}: {stats['kills']}")
    print(f"{'players known':>14}: {stats['players_known']} ({stats['players_pending']} pending)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Valorant match dataset.")
    parser.add_argument("--matches", type=int, default=200, help="how many new matches to store")
    parser.add_argument("--region", default=os.environ.get("RIOT_REGION", "na"))
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES))
    parser.add_argument("--per-player", type=int, default=5, help="matches per player per mode")
    parser.add_argument("--rate", type=int, default=90, help="requests/min budget")
    parser.add_argument("--seed", default="", help="seed from a Riot ID, e.g. Name#TAG")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
