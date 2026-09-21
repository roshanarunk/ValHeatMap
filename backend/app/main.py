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
async def health() -> dict[str, Any]:
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
async def facets() -> dict[str, Any]:
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
async def reference() -> dict[str, Any]:
    return {
        "agents": [a.as_dict() for a in sorted(agents_by_id().values(), key=lambda a: a.name)],
        "weapons": [w.as_dict() for w in sorted(weapons_by_id().values(), key=lambda w: w.name)],
    }


@app.get("/api/maps/{map_name}")
async def map_detail(map_name: str) -> dict[str, Any]:
    info = get_map(map_name)
    if info is None:
        raise HTTPException(404, "Unknown map.")
    return info.as_dict()


# --- core analytics -----------------------------------------------------
@app.get("/api/kills")
async def kills_endpoint(request: Request) -> dict[str, Any]:
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
async def utility_endpoint(request: Request) -> dict[str, Any]:
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
async def plants_endpoint(
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
async def insights_endpoint(request: Request) -> dict[str, Any]:
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
async def dataset_stats() -> dict[str, Any]:
    return {**_db.stats(), "read_only": _READ_ONLY}


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
