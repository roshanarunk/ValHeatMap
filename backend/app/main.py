"""ValHeatMap API.

Serves normalised Valorant match analytics: kill heatmaps with agent/time
filtering, utility-damage maps, plant heatmaps and plant-spot win rates.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .analytics import insights, plants as plant_analytics
from .analytics.kills import (
    EnrichedKill,
    KillFilters,
    apply_filters,
    enrich,
    summarise,
    time_histogram,
    to_points,
)
from .models import DamageType, Match
from .reference import agents_by_id, get_map, weapons_by_id
from .config import load_env
from .crawler import Crawler
from .db import db as match_db
from .sources import clients
from .store import parse_any, store

# Pick up HENRIK_API_KEY / RIOT_API_KEY from .env before anything reads them.
load_env()

app = FastAPI(title="ValHeatMap API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Enriched kills are pure functions of a match, so cache them per match id.
_enriched_cache: dict[tuple[str, int, float], list[EnrichedKill]] = {}


def _enriched(match: Match, window_ms: int, radius: float) -> list[EnrichedKill]:
    key = (match.meta.match_id, window_ms, radius)
    hit = _enriched_cache.get(key)
    if hit is None:
        hit = enrich(match, trade_window_ms=window_ms, trade_radius=radius)
        _enriched_cache[key] = hit
    return hit


def _selected(
    match_ids: str | None, map_name: str | None, mode: str | None
) -> list[Match]:
    ids = [m for m in (match_ids or "").split(",") if m]
    matches = store.select(match_ids=ids or None, map_name=map_name, mode=mode)
    if not matches:
        raise HTTPException(404, "No matches found for that selection.")
    return matches


def _resolve_map(matches: list[Match]):
    map_info = get_map(matches[0].meta.map_id) or get_map(matches[0].meta.map_name)
    if map_info is None or not map_info.has_calibration:
        raise HTTPException(
            422,
            f"No minimap calibration available for '{matches[0].meta.map_name}'.",
        )
    return map_info


def _collect(
    matches: list[Match], request: Request, window_ms: int, radius: float
) -> list[EnrichedKill]:
    filters = KillFilters.from_query(dict(request.query_params))
    out: list[EnrichedKill] = []
    for match in matches:
        out.extend(apply_filters(_enriched(match, window_ms, radius), filters))
    return out


@app.exception_handler(clients.SourceError)
async def _source_error(_: Request, exc: clients.SourceError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


# --- meta ---------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    store.ensure_loaded()
    return {
        "status": "ok",
        "matches": len(store.all()),
        "live_sources": clients.available_sources(),
    }


@app.get("/api/reference")
async def reference() -> dict[str, Any]:
    """Agents, weapons and calibrated maps, for populating filter UI."""
    return {
        "agents": [a.as_dict() for a in sorted(agents_by_id().values(), key=lambda a: a.name)],
        "weapons": [w.as_dict() for w in sorted(weapons_by_id().values(), key=lambda w: w.name)],
    }


@app.get("/api/matches")
async def list_matches() -> dict[str, Any]:
    return {"matches": [m.to_summary() for m in store.all()]}


@app.get("/api/maps")
async def list_maps() -> dict[str, Any]:
    rows = []
    for row in store.maps():
        info = get_map(row["map_id"]) or get_map(row["map_name"])
        rows.append({**row, "minimap": info.minimap if info else "", "splash": info.splash if info else ""})
    return {"maps": rows}


@app.get("/api/matches/{match_id}")
async def get_match(match_id: str) -> dict[str, Any]:
    match = store.get(match_id)
    if match is None:
        raise HTTPException(404, "Match not found.")
    info = get_map(match.meta.map_id) or get_map(match.meta.map_name)
    return {
        "match": match.to_summary(),
        "map": info.as_dict() if info else None,
        "rounds": [
            {
                "number": r.number,
                "winning_team": r.winning_team,
                "result": r.result,
                "kills": len(r.kills),
                "planted": r.plant is not None,
                "plant_site": r.plant.site if r.plant else "",
                "sides": {t: s.value for t, s in r.team_sides.items()},
            }
            for r in match.rounds
        ],
    }


# --- core analytics -----------------------------------------------------
@app.get("/api/kills")
async def kills_endpoint(
    request: Request,
    match_ids: str | None = None,
    map_name: str | None = None,
    mode: str | None = None,
    anchor: str = Query("victim", pattern="^(victim|killer)$"),
    trade_window: int = Query(3000, ge=0, le=10000),
    trade_radius: float = Query(3000.0, ge=0),
) -> dict[str, Any]:
    """Filtered kill points in minimap space, plus headline stats.

    Every filter in `KillFilters` is accepted as a query parameter, e.g.
    `?agents=Jett,Reyna&sides=attack&time_end=30000&traded_only=1`.
    """
    matches = _selected(match_ids, map_name, mode)
    map_info = _resolve_map(matches)
    selected = _collect(matches, request, trade_window, trade_radius)
    return {
        "map": map_info.as_dict(),
        "points": to_points(selected, map_info, anchor=anchor),
        "stats": summarise(selected, matches[0]),
        "histogram": time_histogram(selected),
        "matches": [m.meta.match_id for m in matches],
        "anchor": anchor,
    }


@app.get("/api/utility")
async def utility_endpoint(
    request: Request,
    match_ids: str | None = None,
    map_name: str | None = None,
    mode: str | None = None,
    trade_window: int = Query(3000, ge=0, le=10000),
    trade_radius: float = Query(3000.0, ge=0),
) -> dict[str, Any]:
    """Kills caused by damaging utility, by agent and ability."""
    matches = _selected(match_ids, map_name, mode)
    map_info = _resolve_map(matches)
    selected = [
        ek
        for ek in _collect(matches, request, trade_window, trade_radius)
        if ek.kill.damage_type is DamageType.ABILITY
    ]
    return {
        "map": map_info.as_dict(),
        "points": to_points(selected, map_info, anchor="victim"),
        "report": insights.utility_report(selected, matches[0]),
        "stats": summarise(selected, matches[0]),
    }


@app.get("/api/plants")
async def plants_endpoint(
    match_ids: str | None = None,
    map_name: str | None = None,
    mode: str | None = None,
    sites: str | None = None,
    cluster_radius: float = Query(plant_analytics.CLUSTER_RADIUS, ge=100, le=4000),
    min_sample: int = Query(plant_analytics.MIN_SAMPLE, ge=1, le=50),
) -> dict[str, Any]:
    """Plant locations, clustered plant spots and their round win rates."""
    matches = _selected(match_ids, map_name, mode)
    map_info = _resolve_map(matches)
    wanted = {s for s in (sites or "").split(",") if s}
    all_plants = [p for m in matches for p in m.plants if not wanted or p.site in wanted]
    if not all_plants:
        return {
            "map": map_info.as_dict(),
            "points": [], "spots": [], "sites": [],
            "summary": {"planted_rounds": 0, "plant_win_rate": 0.0},
        }
    spots = plant_analytics.cluster(all_plants, radius=cluster_radius)
    wins = sum(1 for p in all_plants if p.won)
    return {
        "map": map_info.as_dict(),
        "points": plant_analytics.plant_points(all_plants, map_info),
        "spots": plant_analytics.spot_payload(spots, map_info, min_sample=min_sample),
        "sites": plant_analytics.site_breakdown(all_plants),
        "summary": {
            "planted_rounds": len(all_plants),
            "plant_win_rate": round(wins / len(all_plants), 4),
            "defused": sum(1 for p in all_plants if p.defused),
            "spots": len(spots),
        },
    }


@app.get("/api/insights")
async def insights_endpoint(
    request: Request,
    match_ids: str | None = None,
    map_name: str | None = None,
    mode: str | None = None,
    trade_window: int = Query(3000, ge=0, le=10000),
    trade_radius: float = Query(3000.0, ge=0),
    grid: int = Query(insights.ZONE_GRID, ge=4, le=32),
) -> dict[str, Any]:
    """The differentiated stats: trades, opening duels, zones, ranges."""
    matches = _selected(match_ids, map_name, mode)
    map_info = _resolve_map(matches)
    selected = _collect(matches, request, trade_window, trade_radius)
    primary = matches[0]
    return {
        "stats": summarise(selected, primary),
        "trades": insights.trade_report(selected, primary),
        "opening_duels": insights.opening_duels(selected, primary),
        "utility": insights.utility_report(selected, primary),
        "weapons": insights.weapon_report(selected),
        "distance": insights.duel_distance(selected),
        "zones": insights.hot_zones(selected, map_info, grid=grid),
        "timing": insights.timing_profile(selected),
        "multikills": insights.multikill_rounds(selected, primary),
        "opening_impact": insights.economy_of_death(selected, primary),
    }


# --- ingestion ----------------------------------------------------------
def _persist(match: Match, payload: dict[str, Any]) -> None:
    """Index the match and keep its raw payload, so it survives a restart."""
    store.add(match)
    match_db.save_match(
        match.meta.match_id,
        payload,
        {
            "map_name": match.meta.map_name,
            "mode": match.meta.mode,
            "queue": match.meta.queue,
            "region": match.meta.region,
            "started_at": match.meta.started_at,
            "rounds": len(match.rounds),
            "kills": len(match.kills),
            "plants": len(match.plants),
            "source": match.meta.source,
        },
    )


@app.post("/api/import/upload")
async def import_upload(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Ingest a raw match JSON (Riot or HenrikDev shape) into the store."""
    try:
        match = parse_any(payload, source="upload")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not match.meta.match_id:
        raise HTTPException(400, "Match payload has no match id.")
    _persist(match, payload)
    return {"imported": match.meta.match_id, "match": match.to_summary()}


@app.post("/api/import/henrik/{match_id}")
async def import_henrik(match_id: str, region: str | None = None) -> dict[str, Any]:
    payload = await clients.henrik_match(match_id, region)
    match = parse_any(payload, source="henrik")
    if not match.meta.match_id:
        match.meta.match_id = match_id
    _persist(match, payload)
    return {"imported": match.meta.match_id, "match": match.to_summary()}


@app.post("/api/import/riot/{match_id}")
async def import_riot(match_id: str, region: str | None = None) -> dict[str, Any]:
    payload = await clients.riot_match(match_id, region)
    match = parse_any(payload, source="riot")
    if not match.meta.match_id:
        match.meta.match_id = match_id
    _persist(match, payload)
    return {"imported": match.meta.match_id, "match": match.to_summary()}


@app.post("/api/import/henrik/player/{name}/{tag}")
async def import_henrik_player(
    name: str, tag: str, region: str | None = None, mode: str | None = None, size: int = 5
) -> dict[str, Any]:
    """Pull a player's recent matches in one go."""
    payload = await clients.henrik_matchlist(name, tag, region, mode, size)
    data = payload.get("data") or []
    imported: list[str] = []
    for entry in data:
        try:
            match = parse_any(entry, source="henrik")
        except ValueError:
            continue
        if match.meta.match_id:
            _persist(match, entry)
            imported.append(match.meta.match_id)
    if not imported:
        raise HTTPException(422, "No usable matches in the upstream response.")
    return {"imported": imported, "count": len(imported)}


@app.get("/api/dataset")
async def dataset_stats() -> dict[str, Any]:
    """Size and composition of the stored dataset, plus crawl progress."""
    stats = match_db.stats()
    stats["loaded_in_memory"] = len(store.all())
    return stats


@app.post("/api/dataset/reload")
async def dataset_reload() -> dict[str, Any]:
    """Re-read matches from disk to pick up new crawler output."""
    count = store.reload()
    return {"loaded": count}


@app.post("/api/crawl")
async def start_crawl(
    matches: int = Query(50, ge=1, le=5000),
    region: str | None = None,
    seed: str | None = None,
) -> dict[str, Any]:
    """Run a crawl to grow the dataset.

    Blocks until the target is met; with a 90 req/min key, 50 matches takes
    roughly a minute. For large crawls prefer the CLI:
    `python -m app.crawler --matches 2000`.
    """
    key = clients.henrik_key()
    if not key:
        raise HTTPException(400, "HENRIK_API_KEY is not set on the server.")

    crawler = Crawler(
        api_key=key,
        region=region or clients.DEFAULT_REGION,
        rate_limit=int(os.environ.get("HENRIK_RATE_LIMIT", 90)),
        verbose=False,
    )
    if seed and "#" in seed:
        name, _, tag = seed.partition("#")
        async with httpx.AsyncClient(timeout=30) as client:
            await crawler.seed_from_riot_id(client, name, tag)

    result = await crawler.run(target_matches=matches)
    # Make the new matches visible without a restart.
    store.reload()
    return {**result, "dataset": match_db.stats()}


# --- static frontend ----------------------------------------------------
# Serve the built SPA when it exists, so one process hosts the whole app.
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> FileResponse:
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
