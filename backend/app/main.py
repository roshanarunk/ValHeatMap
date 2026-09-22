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

import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

app = FastAPI(title="ValHeatMap API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# On serverless this downloads a published snapshot into the writable temp
# dir; locally it is just the file on disk.
_DB_PATH = ensure_local_db()
_READ_ONLY = os.environ.get("VALHEATMAP_READ_ONLY", "").lower() in {"1", "true", "yes"}
_db = AnalyticsDB(_DB_PATH, read_only=_READ_ONLY)
_engine = QueryEngine(_db)

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
    _maybe_refresh()
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
@app.get("/api/health")
def health() -> dict[str, Any]:
    stats = _db.stats()
    return {
        "status": "ok",
        "matches": stats["matches"],
        "kills": stats["kills"],
        "generated_at": stats["generated_at"],
        "read_only": _READ_ONLY,
        "live_sources": clients.available_sources(),
    }


@app.get("/api/facets")
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
def reference() -> dict[str, Any]:
    return {
        "agents": [a.as_dict() for a in sorted(agents_by_id().values(), key=lambda a: a.name)],
        "weapons": [w.as_dict() for w in sorted(weapons_by_id().values(), key=lambda w: w.name)],
    }


@app.get("/api/maps/{map_name}")
def map_detail(map_name: str) -> dict[str, Any]:
    info = get_map(map_name)
    if info is None:
        raise HTTPException(404, "Unknown map.")
    return info.as_dict()


# --- core analytics -----------------------------------------------------
@app.get("/api/kills")
def kills_endpoint(request: Request) -> dict[str, Any]:
    """Filtered kill points in minimap space, plus headline stats.

    Filters arrive as query parameters, e.g.
    `?map_name=Ascent&agents=Jett&ranks=radiant&acts=e11a5&time_end=30000`.
    """
    f = _filters(request)
    info = _require_map(f)
    result = _engine.kill_points(f)
    return {
        "map": info.as_dict(),
        "points": result["points"],
        "total": result["total"],
        "sampled": result["sampled"],
        "stats": _engine.summary(f),
        "histogram": _engine.histogram(f),
    }


@app.get("/api/utility")
def utility_endpoint(request: Request) -> dict[str, Any]:
    """Kills finished by damaging abilities."""
    f = _filters(request)
    f.utility_only = True
    info = _require_map(f)
    result = _engine.kill_points(f)
    return {
        "map": info.as_dict(),
        "points": result["points"],
        "total": result["total"],
        "sampled": result["sampled"],
        "stats": _engine.summary(f),
        "abilities": _engine.ability_breakdown(f),
    }


@app.get("/api/plants")
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
    payload = plant_analytics.spot_payload(spots, info, min_sample=min_sample)
    # spot_payload projects centroids through to_minimap(); these are
    # already in minimap space, so put the raw centroid back.
    for row, spot in zip(payload, spots):
        cx, cy = spot.centroid
        row["position"] = {"x": round(cx, 4), "y": round(cy, 4)}

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


@app.get("/api/insights")
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
    _engine.invalidate()  # a new player id was interned
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
    _engine.invalidate()
    payload = _player_payload(record["puuid"])
    return {
        "player": payload,
        "stored": stored,
        "new_matches": max(0, payload.get("matches", 0) - before),
    }


@app.get("/api/player/{riot_id:path}/matches")
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
def player_detail(riot_id: str, request: Request) -> dict[str, Any]:
    """Headline stats for a tracked player, narrowed by any filters given."""
    name, tag = _split_riot_id(riot_id)
    record = _db.tracked_player(name, tag)
    if record is None:
        raise HTTPException(404, f"{name}#{tag} is not being tracked yet.")
    # Touch it: the crawler uses last_seen_at to keep active players fresh.
    _db.track_player(record["puuid"], record["name"], record["tag"], record.get("region"))
    return _player_payload(record["puuid"], _filters(request))


@app.get("/api/match/{match_id}")
async def match_detail(match_id: str, request: Request) -> dict[str, Any]:
    """One match, for review: its kills, plants, players and scoreboard.

    Fetches and stores the match if we do not have it, so a game finished
    minutes ago can be reviewed without waiting for the crawler.
    """
    row_id = _db.match_row_id(match_id)
    if row_id is None:
        if _READ_ONLY:
            raise HTTPException(404, "Match not in the dataset.")
        await _ingest_match(match_id)
        row_id = _db.match_row_id(match_id)
        if row_id is None:
            raise HTTPException(404, "Match could not be ingested.")

    detail = _engine.match_detail(match_id)
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

    before = Path(_DB_PATH).stat().st_size if Path(_DB_PATH).exists() else 0
    elapsed = ensure_indexes(Path(_DB_PATH))
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
