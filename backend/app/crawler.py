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
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import load
from .analytics.kills import enrich
from .analytics_db import AnalyticsDB
from .config import load_env
from .db import Database, db as default_db
from .reference import get_map
from .store import parse_any

HENRIK_BASE = "https://api.henrikdev.xyz/valorant"
# Queues worth collecting: these have rounds, plants and sides.
DEFAULT_MODES = ("competitive", "unrated")

# Rebuild the facet cache after this many new matches. Kept below the
# reader's staleness tolerance (500 in AnalyticsDB._facets_are_stale) so
# the crawler always refreshes it before a request finds it stale and
# recomputes the whole thing mid-response.
FACET_REFRESH_MATCHES = 400

# How many pages of `stored-matches` to walk for a tracked player. Measured
# at four pages (366 matches) before the endpoint runs dry, so six leaves
# headroom without risking an unbounded loop if it ever stops paginating.
MAX_HISTORY_PAGES = 6


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


@dataclass
class CrawlControl:
    """Shared pause/stop switch and live status for a running crawl.

    The tray app and the crawl loop run in different threads, so state is
    kept behind `threading` primitives rather than asyncio ones -- an
    asyncio.Event can only be set safely from its own loop.
    """

    _paused: "threading.Event" = field(default_factory=lambda: threading.Event())
    _stopped: "threading.Event" = field(default_factory=lambda: threading.Event())
    status: str = "starting"
    cycle: int = 0
    stored_this_run: int = 0
    db_matches: int = 0
    db_kills: int = 0
    last_publish: str = "never"
    last_error: str = ""

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def stopping(self) -> bool:
        return self._stopped.is_set()

    def pause(self) -> None:
        self._paused.set()
        self.status = "paused"

    def resume(self) -> None:
        self._paused.clear()
        self.status = "running"

    def toggle(self) -> bool:
        if self.paused:
            self.resume()
        else:
            self.pause()
        return self.paused

    def stop(self) -> None:
        self._stopped.set()
        self._paused.clear()
        self.status = "stopping"

    async def wait_while_paused(self) -> None:
        """Yield until resumed. Checked between requests, so a pause takes
        effect within a second rather than at the end of a batch."""
        while self.paused and not self.stopping:
            await asyncio.sleep(0.25)


class Crawler:
    def __init__(
        self,
        api_key: str,
        database: Database | None = None,
        analytics: AnalyticsDB | None = None,
        region: str = "na",
        modes: tuple[str, ...] = DEFAULT_MODES,
        rate_limit: int = 90,
        verbose: bool = True,
        control: "CrawlControl | None" = None,
    ) -> None:
        self.key = api_key
        self.db = database or default_db
        # Matches land in the analytics database as they arrive, so the site
        # never needs a separate rebuild pass to see new data.
        self.analytics = analytics if analytics is not None else AnalyticsDB()
        self.region = region
        self.modes = modes
        self.limiter = RateLimiter(limit=rate_limit, remaining=rate_limit)
        self.verbose = verbose
        self.control = control
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
        attempts = 2
        for attempt in range(attempts):
            if self.control is not None:
                await self.control.wait_while_paused()
                if self.control.stopping:
                    return None
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
                # Usually transient, so retry. But a wrong region answers
                # 500 every time, and silently returning None made that
                # look like a player with no matches -- so say so on the
                # last attempt rather than failing quietly.
                if attempt == attempts - 1:
                    self.log(f"  ! HTTP {resp.status_code} (giving up) for {url}")
                    self.errors += 1
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
        if self.analytics is not None:
            info = get_map(match.meta.map_id) or get_map(match.meta.map_name)
            try:
                self.analytics.add_match(match, info, enrich(match))
            except Exception as exc:  # never let analytics kill a crawl
                self.log(f"  ! analytics write failed for {match_id}: {exc}")
        self.stored += 1

        # Every player in this match is a new crawl candidate.
        for player in match.players:
            self.db.add_player(
                player.puuid, player.name, player.tag,
                match.meta.region or self.region, player.tier,
            )
        return True

    async def crawl_player(
        self, client: httpx.AsyncClient, puuid: str, size: int = 5,
        region: str | None = None,
    ) -> int:
        """Fetch a player's recent matches. Returns how many were stored.

        `region` matters: the matchlist endpoint is per-region and answers
        500 for a player who is not on the one asked for. A registered
        player from another region would otherwise look like a successful
        crawl that stored nothing.
        """
        stored = 0
        region = (region or self.region).lower()
        for mode in self.modes:
            payload = await self._get(
                client,
                f"{HENRIK_BASE}/v4/by-puuid/matches/{region}/pc/{puuid}",
                {"size": size, "mode": mode},
            )
            for raw in (payload or {}).get("data") or []:
                if self._store(raw):
                    stored += 1
        self.db.mark_player_crawled(puuid, stored)
        return stored

    async def crawl_player_history(
        self,
        client: httpx.AsyncClient,
        puuid: str,
        region: str | None = None,
        size: int = 100,
        max_pages: int = MAX_HISTORY_PAGES,
    ) -> int:
        """Fetch a tracked player's deeper history. Returns matches stored.

        The v4 matchlist that `crawl_player` uses returns **at most 10**
        whatever `size` says, which is fine for discovery -- it only needs
        a few games per player to keep snowballing -- but it is the whole
        history for someone looking at their own stats.

        `stored-matches` goes much deeper and paginates: measured at 366
        matches over four pages for a real account, of which 205 were
        competitive. It returns summaries rather than full payloads, so
        each match we do not already have costs one more request to fetch;
        matches already stored cost nothing, which is what makes repeat
        crawls of the same player cheap.
        """
        region = (region or self.region).lower()
        wanted = {m.lower() for m in self.modes}
        stored = 0
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            listing = await self._get(
                client,
                f"{HENRIK_BASE}/v1/by-puuid/stored-matches/{region}/{puuid}",
                {"size": size, "page": page},
            )
            rows = (listing or {}).get("data") or []
            if not rows:
                break  # history exhausted

            page_ids = set()
            for row in rows:
                meta = row.get("meta") or {}
                match_id = meta.get("id")
                if not match_id or match_id in seen:
                    continue
                page_ids.add(match_id)
                seen.add(match_id)
                # Mode names here are display-cased ("Competitive"), unlike
                # the lowercase slugs the v4 endpoint takes.
                if wanted and str(meta.get("mode", "")).lower() not in wanted:
                    continue
                if self.db.has_match(match_id):
                    self.skipped += 1
                    continue
                payload = await self._get(
                    client, f"{HENRIK_BASE}/v4/match/{region}/{match_id}"
                )
                data = (payload or {}).get("data")
                if data and self._store(data):
                    stored += 1

            # A page that repeats what we already listed means the endpoint
            # is ignoring `page`; stop rather than loop over the same rows.
            if not page_ids:
                break
        return stored

    async def crawl_tracked_players(
        self, client: httpx.AsyncClient, limit: int = 5, per_player: int = 10
    ) -> int:
        """Fetch history for people who asked to see their own stats.

        These jump the queue: someone who has just registered is watching
        an empty page, while the discovery crawl is background work that
        nobody is waiting on. It is a small slice of each cycle -- five
        players at ~2 requests each against a 200-match batch -- so it
        costs the general crawl very little.
        """
        if self.analytics is None:
            return 0
        try:
            pending = self.analytics.players_needing_crawl(limit=limit)
        except Exception as exc:  # never let this stop the main crawl
            self.log(f"  ! tracked player lookup failed: {exc}")
            return 0

        stored = 0
        for player in pending:
            try:
                # First crawl goes deep, later ones only need the new games.
                # A backfill is up to ~100 requests, which is a minute of
                # budget spent once; after that the player's history is
                # there and a top-up costs a handful.
                first_time = not player.get("crawled_at")
                if first_time:
                    got = await self.crawl_player_history(
                        client, player["puuid"], region=player.get("region")
                    )
                else:
                    got = await self.crawl_player(
                        client, player["puuid"], per_player, region=player.get("region")
                    )
                stored += got
                self.analytics.mark_player_crawled(player["puuid"], got)
                depth = "history" if first_time else "recent"
                self.log(f"  · tracked {player['name']}#{player['tag']} +{got} ({depth})")
            except Exception as exc:
                # Mark it crawled anyway: a player whose fetch always fails
                # would otherwise be retried forever at the front of the
                # queue, starving everyone behind them.
                self.analytics.mark_player_crawled(player["puuid"])
                self.log(f"  ! tracked {player['name']}#{player['tag']} failed: {exc}")
        return stored

    async def run(self, target_matches: int = 200, per_player: int = 5) -> dict[str, Any]:
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            # Registered players first, before the discovery crawl.
            await self.crawl_tracked_players(client)

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


async def run_forever(
    crawler: "Crawler",
    batch: int = 200,
    publish_every: int = 2000,
    pause_s: float = 0.0,
    control: "CrawlControl | None" = None,
) -> None:
    """Crawl continuously, publishing a snapshot as the dataset grows.

    Serverless functions cannot host this -- they are killed at 300-800s --
    so it runs wherever you keep a long-lived process. Each batch resumes
    from the stored frontier, so stopping and restarting repeats no work.

    A `CrawlControl` lets the tray app pause, resume and stop the loop; the
    pause is also checked between individual requests inside a batch.
    """
    from datetime import datetime, timezone

    from .publish import publish

    control = control or crawler.control
    since_publish = 0
    since_facets = 0
    facet_task: asyncio.Task[None] | None = None
    cycle = 0

    def note(status: str) -> None:
        if control is not None:
            control.status = status

    note("running")
    while True:
        if control is not None:
            if control.stopping:
                note("stopped")
                return
            await control.wait_while_paused()
            if control.stopping:
                note("stopped")
                return

        cycle += 1
        if control is not None:
            control.cycle = cycle

        # Yield more of the shared vCPU and disk I/O to whatever else is
        # running -- chiefly the API process -- when the machine is
        # already busy, or unconditionally during hours real people are
        # likely to be using the site.
        #
        # The reactive checks (load average, free memory, and now real
        # query latency) exist because each one caught an incident the
        # others missed -- but an incident where a plain COUNT(*) on the
        # kills table took 4.8-13s, from disk I/O contention between this
        # process's writes and the API's reads, was invisible to load
        # average and free memory both. A fixed quiet window is a floor
        # under those signals, not a replacement: it throttles even when
        # everything *looks* idle, because "looks idle by these metrics"
        # is exactly what that incident did right up until a real request
        # hung. Longer pause during quiet hours (60s vs 15s) for the same
        # reason -- reacting after the fact was not enough; the point is
        # to leave more headroom before it is needed.
        db_path = str(crawler.analytics.path) if crawler.analytics is not None else None
        quiet = load.in_quiet_hours()
        if quiet or load.is_busy(db_path):
            wait = 60 if quiet else 15
            reason = "quiet hours" if quiet else "machine busy"
            crawler.log(f"  · {reason}, pausing {wait}s before this batch")
            await asyncio.sleep(wait)

        before = crawler.stored
        try:
            await crawler.run(target_matches=before + batch)
        except asyncio.CancelledError:
            note("stopped")
            raise
        except Exception as exc:
            crawler.log(f"  ! crawl cycle failed: {exc}; retrying in 60s")
            if control is not None:
                control.last_error = str(exc)[:120]
                note("retrying")
            await asyncio.sleep(60)
            continue

        gained = crawler.stored - before
        since_publish += gained
        stats = crawler.analytics.stats() if crawler.analytics else {}
        if control is not None:
            control.stored_this_run = crawler.stored
            control.db_matches = stats.get("matches", 0)
            control.db_kills = stats.get("kills", 0)
            if not control.paused:
                note("running")
        crawler.log(
            f"[cycle {cycle}] +{gained} this batch, {crawler.stored} this run, "
            f"db {stats.get('matches', '?')} matches / {stats.get('kills', '?')} kills"
        )

        # Refresh the facet cache here rather than letting a request find
        # it stale. When the crawler and the API share one file -- as they
        # do on a server -- the reader's staleness check otherwise makes
        # some unlucky request recompute over every kill.
        #
        # Run it on a worker thread, not inline: this coroutine is on the
        # crawler's *only* event loop, so a synchronous call here blocks
        # that whole loop until it returns. At 7M+ kills, with no index
        # supporting the agent/weapon/ability GROUP BYs, that call was
        # measured at 139s. On a shared-cpu-1x machine, 139s of one process
        # pegging the single core starves the sibling API process too --
        # its health check missed its 5s window and Fly's proxy pulled the
        # machine out of rotation, which is what actually caused the 503s.
        # A worker thread keeps this loop free to keep crawling and logging
        # while the rebuild runs, so nothing downstream of it stalls.
        since_facets += gained
        if (
            crawler.analytics is not None
            and since_facets >= FACET_REFRESH_MATCHES
            and (facet_task is None or facet_task.done())
        ):
            # Even on a worker thread this is real CPU and disk time --
            # 21s measured on the production database after indexing the
            # group-by columns, down from 139s unindexed, but still enough
            # to matter on one shared vCPU with a My Stats request also
            # in flight. Deferring leaves since_facets where it is, so the
            # next cycle tries again rather than resetting the counter and
            # letting the cache go stale for another 400 matches. Deferred
            # unconditionally during quiet hours too, same reasoning as
            # the batch pause above: this is disk-and-CPU-heavy work that
            # can wait, and "machine looks idle" was already shown not to
            # mean "the disk has headroom right now."
            if quiet or load.is_busy(db_path):
                reason = "quiet hours" if quiet else "machine busy"
                crawler.log(f"  · {reason}, deferring facet cache rebuild")
            else:
                since_facets = 0

                async def _rebuild(analytics: AnalyticsDB) -> None:
                    started = time.monotonic()
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            None, analytics.rebuild_facet_cache
                        )
                        crawler.log(
                            f"  · facet cache rebuilt in {time.monotonic() - started:.0f}s"
                        )
                    except Exception as exc:  # a stale cache is not worth stopping over
                        crawler.log(f"  ! facet cache rebuild failed: {exc}")

                facet_task = asyncio.ensure_future(_rebuild(crawler.analytics))

        if gained == 0:
            # Frontier exhausted or upstream unhappy; back off rather than spin.
            crawler.log("  · nothing new, pausing 120s")
            note("idle")
            await asyncio.sleep(120)
            continue

        if publish_every and since_publish >= publish_every:
            note("publishing")
            try:
                # This process can run for days. Without re-importing, it
                # keeps publishing with whatever publish code it started
                # with -- which is how a 540 MB database reached the site
                # after the slim build had already been written.
                import importlib

                from . import publish as publish_module

                importlib.reload(publish_module)
                publish = publish_module.publish
                crawler.analytics.set_meta(
                    "generated_at",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
                url = publish(verbose=True)
                crawler.log(f"  · published snapshot -> {url}")
                since_publish = 0
                if control is not None:
                    control.last_publish = datetime.now().strftime("%H:%M")
            except Exception as exc:
                crawler.log(f"  ! publish failed: {exc}")
                if control is not None:
                    control.last_error = f"publish: {str(exc)[:100]}"
            note("paused" if (control and control.paused) else "running")

        if pause_s:
            await asyncio.sleep(pause_s)


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

    if args.forever:
        crawler.log(
            f"running continuously: batches of {args.batch}, "
            f"publishing every {args.publish_every} new matches. Ctrl-C to stop."
        )
        try:
            await run_forever(
                crawler,
                batch=args.batch,
                publish_every=args.publish_every,
                pause_s=args.pause,
            )
        except KeyboardInterrupt:
            pass
        return 0

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
    parser.add_argument(
        "--forever", action="store_true", help="crawl continuously until stopped"
    )
    parser.add_argument("--batch", type=int, default=200, help="matches per cycle in --forever")
    parser.add_argument(
        "--publish-every", type=int, default=2000,
        help="publish a snapshot after this many new matches (0 disables)",
    )
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between cycles")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
