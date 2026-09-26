"""ValHeatMap API.

Serves spatial Valorant analytics from a precomputed database: kill
heatmaps with agent/act/rank/time filtering, utility-damage maps, plant
heatmaps and plant-spot win rates.

The API never parses raw match JSON. Every request is answered from
`analytics.db`, which the crawler and `build_analytics` keep current. That
is what makes cold starts viable on a serverless host, where re-reading the
2.5 GB of raw payloads would take ~40s.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import inspect
import json
import os
import threading
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analytics import plants as plant_analytics
from .analytics_db import AnalyticsDB
from .config import load_env
from .models import Plant, Point
from .queries import Filters, QueryEngine
from .reference import agents_by_id, get_map, weapons_by_id
from .snapshot import ensure_local_db, refresh_if_stale
from .sources import clients

load_env()

# Every read-only query endpoint runs its sync DB work through this fixed
# pool rather than as a plain `def` handler.
#
# The first attempt at this used a plain `def` and let Starlette's own
# anyio-managed thread pool handle it, capped via
# anyio.to_thread.current_default_thread_limiter().total_tokens = 6. That
# bounds *concurrency* -- at most 6 can run at once -- but not the total
# number of distinct worker threads created over the process's life:
# anyio spins up a new persistent worker whenever none are idle, reusing
# idle ones for up to 10s before pruning them. AnalyticsDB caches one
# SQLite connection per thread forever via threading.local(), so every
# worker thread that has ever run a query keeps its own open connection
# even after the concurrency cap would refuse it more work. Measured on
# production after that fix: load average down from 7.99 to 1.82 and RSS
# down from 172MB to 117MB (real improvements), but still 17 open
# database file descriptors from one API process, and the health check
# was still failing intermittently.
#
# A ThreadPoolExecutor we own outright has a fixed, known set of worker
# threads -- exactly POOL_SIZE of them, created once, reused for the life
# of the process, never pruned or replaced. That makes the number of
# cached connections a hard, verifiable ceiling instead of a function of
# traffic pattern and GC timing.
POOL_SIZE = 6
_pool = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="query")
_health_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="health")

T = TypeVar("T")


def pooled(fn: Callable[..., T]) -> Callable[..., Any]:
    """Route a plain `def` handler's call through the fixed pool.

    Keeps the handler itself a normal sync function -- easiest to read
    and to unit test directly -- while FastAPI sees an `async def` with
    the *original* signature attached via `__signature__`, so its
    dependency injection (path/query params, Query(), Request, ...)
    still works exactly as it does on any other route. Verified: a
    decorated route's path and query params bind correctly, and the
    call genuinely executes on a `query_N` pool thread, not the request
    that reached the route.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_pool, functools.partial(fn, *args, **kwargs))

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


# On serverless this downloads a published snapshot into the writable temp
# dir; locally it is just the file on disk.
_DB_PATH = ensure_local_db()
_READ_ONLY = os.environ.get("VALHEATMAP_READ_ONLY", "").lower() in {"1", "true", "yes"}
_db = AnalyticsDB(_DB_PATH, read_only=_READ_ONLY)
_engine = QueryEngine(_db)


def _warm_core_queries() -> dict[str, Any]:
    """Pre-warm query engine cache for all default map views.
    Ensures instant map switching across all 11 maps without saturating
    disk I/O or starving live HTTP requests.
    """
    global _engine, _db
    t0 = time.monotonic()
    warmed = 0

    # Ensure Warden is registered in dim table so its ID exists
    try:
        with _db.connect() as conn:
            _db._dim_id(conn, "weapon", "Warden")
    except Exception:
        pass

    try:
        known_maps = set(_db.dim_names("map").values())
    except Exception:
        known_maps = set()

    core_order = [
        "Ascent", "Bind", "Haven", "Split", "Lotus",
        "Sunset", "Breeze", "Icebox", "Abyss", "Pearl", "Fracture"
    ]
    map_names = [m for m in core_order if m in known_maps] or core_order

    for m in map_names:
        try:
            f = Filters(map_name=m)
            info = get_map(m)
            if info is None or not info.has_calibration:
                continue
            _cached_body("kills", f, lambda: _kills_payload(f, info))
            warmed += 1
        except Exception:
            pass
        time.sleep(0.3)  # Cooperative pause for disk I/O and live requests

    elapsed = round(time.monotonic() - t0, 2)
    print(f"[cache_warmer] Complete: pre-warmed {warmed} default maps in {elapsed}s", flush=True)
    return {"warmed": warmed, "seconds": elapsed}


async def _background_warmer_loop() -> None:
    # Brief initial pause to let server bind and health checks pass
    await asyncio.sleep(2.0)
    while True:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _warm_core_queries)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[cache_warmer] warming error: {exc}")

        try:
            await asyncio.sleep(1200)  # Refresh every 20 minutes
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    warmer_task = None
    if (
        os.environ.get("VALHEATMAP_WARM_CACHE", "1").lower() not in {"0", "false", "no"}
        and "pytest" not in sys.modules
        and not os.environ.get("PYTEST_CURRENT_TEST")
    ):
        warmer_task = asyncio.create_task(_background_warmer_loop())
    try:
        yield
    finally:
        if warmer_task is not None:
            warmer_task.cancel()
            try:
                await warmer_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="ValHeatMap API", version="2.0.0", lifespan=lifespan)

# A heatmap is 15k points: 4.7 MB of JSON, 0.38 MB gzipped. Neither
# uvicorn nor Fly's proxy compresses on its own. Responses that arrive
# already gzipped (the cached ones below) pass through untouched.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_cache_headers(request: Request, call_next: Callable[..., Any]) -> Response:
    response: Response = await call_next(request)
    if request.method == "GET" and request.url.path.startswith("/api/"):
        if request.url.path == "/api/health":
            response.headers["Cache-Control"] = "no-cache, no-store"
        elif "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "public, max-age=60"
    return response

# How often a warm instance re-checks the published snapshot. A Vercel
# instance can live for hours, so without this it would serve whatever it
# downloaded at cold start forever -- the site would only pick up new data
# when Vercel happened to spin up a new instance.
REFRESH_INTERVAL_S = int(os.environ.get("VALHEATMAP_REFRESH_SECONDS", "120"))
_last_refresh_check = time.monotonic()
_refresh_lock = threading.Lock()


def _maybe_refresh() -> None:
    """Re-download the snapshot when a newer one has been published.

    Cheap in the common case: a conditional HEAD against the ETag, at most
    once per REFRESH_INTERVAL_S. When the ETag has changed, the database is
    re-downloaded and the connection and cached dimension ids are rebound --
    keeping the old AnalyticsDB would go on serving the previous file even
    after a successful download.
    """
    global _db, _engine, _last_refresh_check

    if not _READ_ONLY:
        return  # local runs read the live file directly
    now = time.monotonic()
    if now - _last_refresh_check < REFRESH_INTERVAL_S:
        return
    if not _refresh_lock.acquire(blocking=False):
        return  # another request is already checking
    try:
        _last_refresh_check = now
        if not refresh_if_stale():
            return
        # Open and sanity-check the new file *before* swapping it in. The
        # download replaces the path in place, and a connection opened with
        # immutable=1 keeps reading the file it was opened on, so a failed
        # or partial refresh must never become the live database.
        fresh = AnalyticsDB(_DB_PATH, read_only=True)
        stats = fresh.stats()
        if not stats.get("matches"):
            print("[snapshot] refreshed file has no matches; keeping the old one")
            return
        _db = fresh
        _engine = QueryEngine(fresh)
        print(f"[snapshot] refreshed to {stats['matches']:,} matches")
    except Exception as exc:  # never fail a request over a refresh
        print(f"[snapshot] refresh failed: {exc}")
    finally:
        _refresh_lock.release()


@app.middleware("http")
async def _refresh_middleware(request: Request, call_next):
    if _READ_ONLY:
        now = time.monotonic()
        if now - _last_refresh_check >= REFRESH_INTERVAL_S:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _maybe_refresh)
    return await call_next(request)


def _filters(request: Request) -> Filters:
    return Filters.from_query(dict(request.query_params))


def _require_map(f: Filters):
    if not f.map_name:
        raise HTTPException(400, "map_name is required.")
    info = get_map(f.map_name)
    if info is None or not info.has_calibration:
        raise HTTPException(422, f"No minimap calibration for '{f.map_name}'.")
    return info


@app.exception_handler(clients.SourceError)
async def _source_error(_: Request, exc: clients.SourceError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


# --- meta ---------------------------------------------------------------
def _health_payload() -> dict[str, Any]:
    _db.ping()
    stats = _db.stats()
    return {
        "status": "ok",
        "matches": stats.get("matches", 0),
        "kills": stats.get("kills", 0),
        "generated_at": stats.get("generated_at"),
        "read_only": _READ_ONLY,
        "live_sources": clients.available_sources(),
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Fly polls this every 30s and pulls the machine out of rotation on
    timeout, so it must never wait behind the same pool as the
    heavier player/kills queries. It runs on a dedicated _health_pool
    so it answers instantly regardless of query traffic.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_health_pool, _health_payload)


@app.get("/api/facets")
@pooled
def facets() -> dict[str, Any]:
    """Everything the UI needs to populate its filter controls."""
    data = _db.facets()
    for row in data["maps"]:
        info = get_map(row["map_name"])
        row["minimap"] = info.minimap if info else ""
        row["splash"] = info.splash if info else ""
    agent_meta = {a.name: a for a in agents_by_id().values()}
    for row in data["agents"]:
        info = agent_meta.get(row["agent"])
        row["icon"] = info.icon if info else ""
        row["role"] = info.role if info else ""
    # Agent roles present in the data, with how many kills each accounts
    # for. Derived from the agent rows rather than stored, so a Riot
    # rework that changes an agent's role is picked up automatically.
    role_kills: dict[str, int] = {}
    for row in data["agents"]:
        role = row.get("role") or ""
        if role:
            role_kills[role] = role_kills.get(role, 0) + row.get("kills", 0)
    data["roles"] = [
        {"role": role, "kills": kills}
        for role, kills in sorted(role_kills.items(), key=lambda kv: -kv[1])
    ]
    # Rank bands, highest first -- these mirror queries.TIER_BANDS.
    data["ranks"] = [
        {"id": "radiant", "name": "Radiant", "tiers": [27, 27]},
        {"id": "immortal", "name": "Immortal", "tiers": [24, 26]},
        {"id": "ascendant", "name": "Ascendant", "tiers": [21, 23]},
        {"id": "diamond", "name": "Diamond", "tiers": [18, 20]},
        {"id": "platinum", "name": "Platinum", "tiers": [15, 17]},
    ]
    data["stats"] = _db.stats()
    return data


@app.get("/api/reference")
@pooled
def reference() -> dict[str, Any]:
    return {
        "agents": [a.as_dict() for a in sorted(agents_by_id().values(), key=lambda a: a.name)],
        "weapons": [w.as_dict() for w in sorted(weapons_by_id().values(), key=lambda w: w.name)],
    }


@app.get("/api/maps/{map_name}")
@pooled
def map_detail(map_name: str) -> dict[str, Any]:
    info = get_map(map_name)
    if info is None:
        raise HTTPException(404, "Unknown map.")
    return info.as_dict()


# --- core analytics -----------------------------------------------------
def _cached_body(kind: str, f: Filters, build: Callable[[], dict[str, Any]]) -> bytes:
    """The gzipped JSON for this response, from the engine's cache or built now.

    Serialised the way FastAPI's JSONResponse does it, so a cached
    response is byte-for-byte what the route would have returned.
    """
    def encode() -> bytes:
        raw = json.dumps(
            build(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        return gzip.compress(raw, compresslevel=6)

    return _engine.cached(_engine.cache_key(kind, f), encode)


def _gzip_response(request: Request, body: bytes) -> Response:
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(
            body,
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
        )
    return Response(gzip.decompress(body), media_type="application/json")


def _kills_payload(f: Filters, info: Any) -> dict[str, Any]:
    result = _engine.kill_points(f)
    return {
        "map": info.as_dict(),
        "points": result["points"],
        "total": result["total"],
        "sampled": result["sampled"],
        "stats": result["stats"],
        "histogram": result["histogram"],
    }


@app.get("/api/kills")
@pooled
def kills_endpoint(request: Request) -> Response:
    """Filtered kill points in minimap space, plus headline stats.

    Filters arrive as query parameters, e.g.
    `?map_name=Ascent&agents=Jett&ranks=radiant&acts=e11a5&time_end=30000`.
    """
    f = _filters(request)
    info = _require_map(f)
    return _gzip_response(request, _cached_body("kills", f, lambda: _kills_payload(f, info)))


@app.get("/api/utility")
@pooled
def utility_endpoint(request: Request) -> Response:
    """Kills finished by damaging abilities."""
    f = _filters(request)
    f.utility_only = True
    info = _require_map(f)

    def build() -> dict[str, Any]:
        result = _engine.kill_points(f)
        return {
            "map": info.as_dict(),
            "points": result["points"],
            "total": result["total"],
            "sampled": result["sampled"],
            "stats": result["stats"],
            "abilities": _engine.ability_breakdown(f),
        }

    return _gzip_response(request, _cached_body("utility", f, build))


@app.get("/api/plants")
@pooled
def plants_endpoint(
    request: Request,
    cluster_radius: float = Query(plant_analytics.CLUSTER_RADIUS, ge=100, le=4000),
    min_sample: int = Query(plant_analytics.MIN_SAMPLE, ge=1, le=500),
) -> dict[str, Any]:
    """Plant locations, clustered spots and their round win rates."""
    f = _filters(request)
    info = _require_map(f)
    raw = _engine.plants(f)
    if not raw:
        return {
            "map": info.as_dict(),
            "points": [], "spots": [], "sites": [],
            "summary": {"planted_rounds": 0, "plant_win_rate": 0.0, "defused": 0, "spots": 0},
        }

    # Stored positions are already in minimap space, so the clustering
    # radius (given in world units) has to be scaled into that space too.
    scale = abs(info.x_multiplier) or 1.0
    plants = [
        Plant(
            round_num=p["round"], round_time_ms=p["t"], site=p["site"],
            location=Point(p["position"]["x"], p["position"]["y"]),
            planter_puuid="", planter_team="",
            won=p["won"], defused=p["defused"],
        )
        for p in raw
    ]
    spots = plant_analytics.cluster(plants, radius=cluster_radius * scale)
    payload = plant_analytics.spot_payload(
        spots, info, min_sample=min_sample, is_minimap_coords=True
    )

    wins = sum(1 for p in raw if p["won"])
    return {
        "map": info.as_dict(),
        "points": raw,
        "spots": payload,
        "sites": plant_analytics.site_breakdown(plants),
        "summary": {
            "planted_rounds": len(raw),
            "plant_win_rate": round(wins / len(raw), 4),
            "defused": sum(1 for p in raw if p["defused"]),
            "spots": len(spots),
        },
    }


@app.get("/api/rotations")
@pooled
def rotations_endpoint(
    map_name: str | None = Query(None, description="Map display name or ID"),
    side: str = Query("defense", description="defense | attack | all"),
    player: str | None = Query(None, description="PUUID or name#tag"),
    agent: str | None = Query(None, description="Agent display name"),
    match_id: str | None = Query(None, description="Match ID to scope rotations to a single match"),
    team: str | None = Query(None, description="Team side or name (Red / Blue)"),
    round_num: int | None = Query(None, description="Specific round number"),
    min_count: int = Query(5, ge=1, le=1000, description="Minimum transition count threshold"),
    focus_zone: str | None = Query(None, description="Optional zone to filter on"),
) -> dict[str, Any]:
    """Macro-rotation transition graph between map zones."""
    if not map_name and not match_id:
        raise HTTPException(400, "Either map_name or match_id must be provided.")
    res = _engine.rotations(
        map_name=map_name,
        side=side,
        player=player,
        agent=agent,
        match_id=match_id,
        team=team,
        round_num=round_num,
        min_count=min_count,
        focus_zone=focus_zone,
    )
    resolved_map = res.get("map_name") or map_name
    if resolved_map:
        info = get_map(resolved_map)
        if info is not None and info.has_calibration:
            res["map"] = info.as_dict()
    return res


@app.get("/api/insights")
@pooled
def insights_endpoint(request: Request) -> dict[str, Any]:
    """Aggregate breakdowns for the current selection."""
    f = _filters(request)
    _require_map(f)
    return {
        "stats": _engine.summary(f),
        "agents": _engine.agent_breakdown(f),
        "weapons": _engine.weapon_breakdown(f),
        "abilities": _engine.ability_breakdown(f),
        "histogram": _engine.histogram(f, bucket_ms=10_000),
    }


# --- players ------------------------------------------------------------
def _split_riot_id(riot_id: str) -> tuple[str, str]:
    """"Name#TAG" -> (name, tag). Names may contain spaces, tags may not."""
    raw = (riot_id or "").strip().lstrip("#")
    if "#" not in raw:
        raise HTTPException(400, "Riot ID must look like Name#TAG.")
    name, _, tag = raw.rpartition("#")
    name, tag = name.strip(), tag.strip()
    if not name or not tag:
        raise HTTPException(400, "Riot ID must look like Name#TAG.")
    return name, tag


@app.post("/api/player/register")
async def register_player(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Start tracking a Riot ID, so the crawler prioritises their matches.

    Resolving the account is what validates the ID: a typo comes back as
    404 from upstream rather than as a player who never gets any data.
    """
    if _READ_ONLY:
        raise HTTPException(409, "This deployment is read-only.")
    name, tag = _split_riot_id(str(payload.get("riot_id") or ""))

    known = _db.tracked_player(name, tag)
    if known:
        # Already tracked: touch it so the page reflects the visit, but do
        # not spend an upstream request re-resolving a known puuid.
        _db.track_player(known["puuid"], known["name"], known["tag"], known.get("region"))
        return {"player": _player_payload(known["puuid"]), "new": False}

    account = await clients.henrik_account(name, tag)
    record = _db.track_player(
        account["puuid"],
        account.get("name") or name,
        account.get("tag") or tag,
        account.get("region"),
    )
    _engine.invalidate(players_only=True)  # a new player id was interned
    return {"player": _player_payload(record["puuid"]), "new": True}


def _player_payload(puuid: str, f: Filters | None = None) -> dict[str, Any]:
    """Everything the player tab needs about one tracked player.

    `f` narrows the headline numbers to the current selection, so the
    tiles describe what is on screen rather than always a career total.
    The map list stays unfiltered: it is the picker, and filtering it by
    the selected map would leave one entry.
    """
    record = _db.tracked_by_puuid(puuid) or {}
    summary = _engine.player_summary(puuid, f)
    return {
        "puuid": puuid,
        "name": record.get("name", ""),
        "tag": record.get("tag", ""),
        "region": record.get("region"),
        "riot_id": f"{record.get('name', '')}#{record.get('tag', '')}",
        "crawled_at": record.get("crawled_at"),
        "requested_at": record.get("requested_at"),
        # Which maps they actually play, so the tab can open on one that
        # has data rather than making them guess.
        "maps": _engine.player_maps(puuid),
        **summary,
    }


@app.post("/api/player/{riot_id:path}/refresh")
async def refresh_player(riot_id: str) -> dict[str, Any]:
    """Pull this player's latest matches now, rather than on the next cycle.

    Runs the fetch inline instead of only requeuing: the caller is a
    person waiting on a button, and telling them "queued" when the
    crawler is mid-batch would mean an indeterminate wait.
    """
    if _READ_ONLY:
        raise HTTPException(409, "This deployment is read-only.")
    name, tag = _split_riot_id(riot_id)
    record = _db.tracked_player(name, tag)
    if record is None:
        raise HTTPException(404, f"{name}#{tag} is not being tracked yet.")

    from .crawler import Crawler

    key = clients.henrik_key()
    if not key:
        raise HTTPException(400, "HENRIK_API_KEY is not set on the server.")

    crawler = Crawler(api_key=key, analytics=_db, region=record.get("region") or "na")
    before = _engine.player_summary(record["puuid"]).get("matches", 0)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0)) as client:
            # Never crawled: go deep. Otherwise just top up the recent ones,
            # which is a handful of requests rather than a few hundred.
            if record.get("crawled_at"):
                stored = await crawler.crawl_player(
                    client, record["puuid"], size=10, region=record.get("region")
                )
            else:
                stored = await crawler.crawl_player_history(
                    client, record["puuid"], region=record.get("region")
                )
    except Exception as exc:
        raise HTTPException(502, f"Refresh failed: {exc}") from exc

    _db.mark_player_crawled(record["puuid"], stored)
    _engine.invalidate(players_only=True)
    payload = _player_payload(record["puuid"])
    return {
        "player": payload,
        "stored": stored,
        "new_matches": max(0, payload.get("matches", 0) - before),
    }


@app.get("/api/player/{riot_id:path}/matches")
@pooled
def player_matches(
    riot_id: str, request: Request, limit: int = 20
) -> dict[str, Any]:
    """A tracked player's matches, newest first, for the review list."""
    name, tag = _split_riot_id(riot_id)
    record = _db.tracked_player(name, tag)
    if record is None:
        raise HTTPException(404, f"{name}#{tag} is not being tracked yet.")
    return {
        "player": _player_payload(record["puuid"], _filters(request)),
        "matches": _engine.player_matches(record["puuid"], limit=limit),
    }


@app.get("/api/player/{riot_id:path}")
@pooled
def player_detail(riot_id: str, request: Request) -> dict[str, Any]:
    """Headline stats for a tracked player, narrowed by any filters given."""
    name, tag = _split_riot_id(riot_id)
    record = _db.tracked_player(name, tag)
    if record is None:
        raise HTTPException(404, f"{name}#{tag} is not being tracked yet.")
    # Touch it: the crawler uses last_seen_at to keep active players fresh.
    _db.track_player(record["puuid"], record["name"], record["tag"], record.get("region"))
    return _player_payload(record["puuid"], _filters(request))


# --- scouting -----------------------------------------------------------
async def _resolve_and_scout(
    riot_id: str,
    map_name: str,
    agent: str | None = None,
) -> dict[str, Any]:
    from .analytics.scout import scout_player

    try:
        name, tag = _split_riot_id(riot_id)
    except HTTPException as e:
        return {
            "riot_id": riot_id,
            "found": False,
            "has_data": False,
            "error": str(e.detail),
            "matches_on_map": 0,
            "map_name": map_name,
            "agent": agent or "",
            "top_agents": [],
            "tactical_tags": [],
            "counter_tips": ["Invalid Riot ID format (must be Name#TAG)."],
            "defense_rotations": [],
            "attack_rotations": [],
            "first_blood_points": [],
        }

    # Check if tracked
    record = _db.tracked_player(name, tag)
    puuid = record["puuid"] if record else None
    region = record.get("region") if record else None

    # If not tracked, try resolving via HenrikDev
    if not record and not _READ_ONLY and clients.henrik_key():
        try:
            account = await clients.henrik_account(name, tag)
            puuid = account.get("puuid")
            region = account.get("region") or "na"
            if puuid:
                record = _db.track_player(
                    puuid,
                    account.get("name") or name,
                    account.get("tag") or tag,
                    region,
                )
                _engine.invalidate(players_only=True)
        except Exception:
            pass

    if not puuid:
        # Check tracked_players table directly
        with _db.connect() as conn:
            p_row = conn.execute(
                "SELECT puuid, region FROM tracked_players WHERE LOWER(name)=LOWER(?) AND LOWER(tag)=LOWER(?)",
                (name, tag),
            ).fetchone()
            if p_row:
                puuid = p_row["puuid"]
                region = p_row["region"]

    if not puuid:
        return {
            "riot_id": f"{name}#{tag}",
            "name": name,
            "tag": tag,
            "found": False,
            "has_data": False,
            "error": f"Player {name}#{tag} not found in database or Riot upstream.",
            "matches_on_map": 0,
            "map_name": map_name,
            "agent": agent or "",
            "top_agents": [],
            "tactical_tags": [],
            "counter_tips": ["Opponent not found. Check spelling of Name#TAG."],
            "defense_rotations": [],
            "attack_rotations": [],
            "first_blood_points": [],
        }

    loop = asyncio.get_running_loop()
    summary = await loop.run_in_executor(
        _pool,
        functools.partial(_engine.player_summary, puuid, Filters(map_name=map_name)),
    )
    if summary.get("matches", 0) == 0 and not _READ_ONLY and clients.henrik_key() and record and not record.get("crawled_at"):
        from .crawler import Crawler
        crawler = Crawler(api_key=clients.henrik_key(), analytics=_db, region=region or "na")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                await crawler.crawl_player(client, puuid, size=5, region=region)
                _db.mark_player_crawled(puuid, 5)
                _engine.invalidate(players_only=True)
        except Exception:
            pass

    report = await loop.run_in_executor(
        _pool,
        functools.partial(
            scout_player,
            _db,
            _engine,
            puuid,
            name,
            tag,
            region,
            map_name,
            agent,
        ),
    )
    return report


@app.post("/api/scout")
async def scout_lobby_endpoint(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Scout up to 5 opponents for an upcoming match on a map."""
    from .analytics.scout import generate_lobby_summary

    map_name = str(payload.get("map_name") or "Ascent")
    opponents_raw = payload.get("opponents") or []
    if not isinstance(opponents_raw, list):
        raise HTTPException(400, "opponents must be a list of player objects.")

    opponents = opponents_raw[:5]
    reports = []
    for opp in opponents:
        rid = str(opp.get("riot_id") or "").strip()
        if not rid:
            continue
        ag = opp.get("agent") or None
        rep = await _resolve_and_scout(rid, map_name, ag)
        reports.append(rep)

    summary = generate_lobby_summary(reports)
    return {
        "map_name": map_name,
        "lobby_summary": summary,
        "reports": reports,
    }


@app.get("/api/scout/player")
async def scout_player_endpoint(
    riot_id: str = Query(..., description="Name#TAG"),
    map_name: str = Query("Ascent", description="Map name"),
    agent: str | None = Query(None, description="Optional agent name"),
) -> dict[str, Any]:
    """Single opponent scouting report."""
    return await _resolve_and_scout(riot_id, map_name, agent)


@app.get("/api/match/{match_id}")
async def match_detail(match_id: str, request: Request) -> dict[str, Any]:
    """One match, for review: its kills, plants, players and scoreboard.

    Fetches and stores the match if we do not have it, so a game finished
    minutes ago can be reviewed without waiting for the crawler.
    """
    loop = asyncio.get_running_loop()
    row_id = await loop.run_in_executor(_pool, _db.match_row_id, match_id)
    if row_id is None:
        if _READ_ONLY:
            raise HTTPException(404, "Match not in the dataset.")
        await _ingest_match(match_id)
        row_id = await loop.run_in_executor(_pool, _db.match_row_id, match_id)
        if row_id is None:
            raise HTTPException(404, "Match could not be ingested.")

    detail = await loop.run_in_executor(_pool, _engine.match_detail, match_id)
    if detail is None:
        raise HTTPException(404, "Match not in the dataset.")
    # The map's calibration and callouts travel with the match: the client
    # needs both to draw it, and asking for them separately would be a
    # second round trip for something we already know here.
    info = get_map(detail["map_name"])
    detail["map"] = info.as_dict() if info else None
    return detail


async def _ingest_match(match_id: str, region: str | None = None) -> int:
    """Pull one match from upstream and store it. Returns kills written."""
    from .analytics.kills import enrich
    from .store import parse_any

    payload = await clients.henrik_match(match_id, region)
    match = parse_any(payload, source="henrik")
    if not match.meta.match_id:
        match.meta.match_id = match_id
    info = get_map(match.meta.map_id) or get_map(match.meta.map_name)
    if info is None or not info.has_calibration:
        raise HTTPException(422, "This map has no coordinate calibration.")
    kills = _db.add_match(match, info, enrich(match))
    _engine.invalidate()
    return kills


# --- ingestion ----------------------------------------------------------
@app.post("/api/import/upload")
async def import_upload(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Ingest a raw match JSON straight into the analytics database."""
    if _READ_ONLY:
        raise HTTPException(409, "This deployment is read-only.")
    from .analytics.kills import enrich
    from .store import parse_any

    try:
        match = parse_any(payload, source="upload")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not match.meta.match_id:
        raise HTTPException(400, "Match payload has no match id.")
    info = get_map(match.meta.map_id) or get_map(match.meta.map_name)
    kills = _db.add_match(match, info, enrich(match))
    _engine.invalidate()
    return {"imported": match.meta.match_id, "kills": kills}


@app.get("/api/dataset")
@pooled
def dataset_stats() -> dict[str, Any]:
    return {**_db.stats(), "read_only": _READ_ONLY}


@app.get("/api/debug/snapshot")
async def debug_snapshot() -> dict[str, Any]:
    """Why the served snapshot is (or is not) current.

    Staleness has several possible causes -- the origin, the CDN edge, the
    instance's cached copy, or the refresh timer -- and they are hard to
    tell apart from outside. This reports all of them at once.
    """
    from . import snapshot as snap

    cached = snap.cached_etag()
    remote = snap.remote_etag()
    return {
        "serving": _db.stats(),
        "db_path": str(_DB_PATH),
        "db_exists": Path(_DB_PATH).exists(),
        "db_size": Path(_DB_PATH).stat().st_size if Path(_DB_PATH).exists() else 0,
        "cache_dir": str(snap.CACHE_DIR),
        "cached_db": str(snap.CACHED_DB),
        "cached_db_exists": snap.CACHED_DB.exists(),
        "etag_file": str(snap.ETAG_FILE),
        "etag_cached": cached,
        "etag_remote": remote,
        "is_stale": bool(remote and remote != cached),
        "snapshot_url": snap.snapshot_url(),
        "refresh_interval_s": REFRESH_INTERVAL_S,
        "seconds_since_check": round(time.monotonic() - _last_refresh_check, 1),
        "read_only": _READ_ONLY,
        "tmp": _tmp_space(),
    }


def _tmp_space() -> dict[str, Any]:
    """How much room the cache directory actually has.

    The published size is bounded by this, and the real figure is worth
    measuring rather than assuming -- providers differ, and the limit is
    what decides how much of the dataset can be served.
    """
    import shutil

    from . import snapshot as snap

    try:
        usage = shutil.disk_usage(snap.CACHE_DIR)
        return {
            "total_mb": round(usage.total / 1e6),
            "used_mb": round(usage.used / 1e6),
            "free_mb": round(usage.free / 1e6),
        }
    except OSError as exc:
        return {"error": str(exc)}


@app.post("/api/debug/reindex")
async def debug_reindex() -> dict[str, Any]:
    """Build any missing indexes on the cached database.

    A deploy can land while an instance already holds a correctly-sized
    snapshot. The ETag still matches, so no refresh is triggered, and the
    file keeps whatever index set the previous code built -- leaving, for
    example, zone queries scanning. This repairs it without re-downloading.
    """
    global _db, _engine
    import sqlite3

    from .slim import ensure_indexes

    loop = asyncio.get_running_loop()
    before = Path(_DB_PATH).stat().st_size if Path(_DB_PATH).exists() else 0
    elapsed = await loop.run_in_executor(None, ensure_indexes, Path(_DB_PATH))
    after = Path(_DB_PATH).stat().st_size

    with sqlite3.connect(f"file:{_DB_PATH}?immutable=1", uri=True) as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            )
        ]

    # Rebind so the engine picks up the new query plans.
    fresh = AnalyticsDB(_DB_PATH, read_only=_READ_ONLY)
    _db = fresh
    _engine = QueryEngine(fresh)
    return {
        "before_mb": round(before / 1e6),
        "after_mb": round(after / 1e6),
        "seconds": round(elapsed, 1),
        "indexes": names,
    }


@app.post("/api/debug/refresh")
async def debug_refresh() -> dict[str, Any]:
    """Force a refresh check immediately, ignoring the interval."""
    global _last_refresh_check

    before = _db.stats().get("matches", 0)
    _last_refresh_check = time.monotonic() - REFRESH_INTERVAL_S - 1
    _maybe_refresh()
    after = _db.stats().get("matches", 0)
    return {"before": before, "after": after, "changed": after != before}


@app.post("/api/debug/warm")
async def debug_warm() -> dict[str, Any]:
    """Manually trigger a cache warm run."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _warm_core_queries)


@app.get("/api/debug/cache")
async def debug_cache() -> dict[str, Any]:
    """Return status of in-memory query cache."""
    with _engine._cache_lock:
        entries = list(_engine._query_cache.items())
        return {
            "entries": len(entries),
            "max_entries": _engine._cache_max_entries,
            "bytes": sum(len(body) for _, body in entries),
            "cached_queries": [
                {"kind": k[0], "where": k[1], "args": list(k[2]), "player": k[4]}
                for k, _ in entries
            ],
        }


# --- static frontend ----------------------------------------------------
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> FileResponse:
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
