"""Live API clients: HenrikDev (unofficial) and Riot official.

HenrikDev is the default because its keys are obtainable today. The Riot
client is wired up and ready for when a production match-v1 key arrives --
both return payloads that `store.parse_any` normalises identically.

Configure with environment variables:
    HENRIK_API_KEY   HenrikDev key (https://api.henrikdev.xyz/dashboard)
    RIOT_API_KEY     official Riot key
    RIOT_REGION      ap | na | eu | kr  (default: na)
"""

from __future__ import annotations

import os
from typing import Any

import httpx

HENRIK_BASE = "https://api.henrikdev.xyz/valorant"
RIOT_SHARDS = {
    "na": "https://na.api.riotgames.com",
    "eu": "https://eu.api.riotgames.com",
    "ap": "https://ap.api.riotgames.com",
    "kr": "https://kr.api.riotgames.com",
    "latam": "https://latam.api.riotgames.com",
    "br": "https://br.api.riotgames.com",
}
DEFAULT_REGION = os.environ.get("RIOT_REGION", "na").lower()
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class SourceError(RuntimeError):
    """Raised with a user-presentable message when an upstream call fails."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def henrik_key() -> str | None:
    return os.environ.get("HENRIK_API_KEY") or None


def riot_key() -> str | None:
    return os.environ.get("RIOT_API_KEY") or None


def available_sources() -> dict[str, bool]:
    return {"henrik": bool(henrik_key()), "riot": bool(riot_key())}


async def _get_json(url: str, headers: dict[str, str], params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.get(url, headers=headers, params=params)
        except httpx.RequestError as exc:
            raise SourceError(f"Upstream request failed: {exc}") from exc
    if resp.status_code == 401 or resp.status_code == 403:
        raise SourceError("Upstream rejected the API key (401/403). Check your key.", 401)
    if resp.status_code == 404:
        raise SourceError("Not found upstream.", 404)
    if resp.status_code == 429:
        raise SourceError("Rate limited by the upstream API. Try again shortly.", 429)
    if resp.status_code >= 400:
        raise SourceError(f"Upstream returned HTTP {resp.status_code}.", 502)
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError("Upstream returned a non-JSON response.") from exc


# --- HenrikDev ---------------------------------------------------------
async def henrik_match(match_id: str, region: str | None = None) -> dict[str, Any]:
    key = henrik_key()
    if not key:
        raise SourceError("HENRIK_API_KEY is not set on the server.", 400)
    region = (region or DEFAULT_REGION).lower()
    url = f"{HENRIK_BASE}/v4/match/{region}/{match_id}"
    return await _get_json(url, {"Authorization": key})


async def henrik_matchlist(
    name: str, tag: str, region: str | None = None, mode: str | None = None, size: int = 5
) -> dict[str, Any]:
    key = henrik_key()
    if not key:
        raise SourceError("HENRIK_API_KEY is not set on the server.", 400)
    region = (region or DEFAULT_REGION).lower()
    url = f"{HENRIK_BASE}/v4/matches/{region}/pc/{name}/{tag}"
    params: dict[str, Any] = {"size": max(1, min(size, 10))}
    if mode:
        params["mode"] = mode
    return await _get_json(url, {"Authorization": key}, params)


# --- Riot official -----------------------------------------------------
async def riot_match(match_id: str, region: str | None = None) -> dict[str, Any]:
    key = riot_key()
    if not key:
        raise SourceError("RIOT_API_KEY is not set on the server.", 400)
    shard = RIOT_SHARDS.get((region or DEFAULT_REGION).lower(), RIOT_SHARDS["na"])
    url = f"{shard}/val/match/v1/matches/{match_id}"
    return await _get_json(url, {"X-Riot-Token": key})


async def riot_matchlist(puuid: str, region: str | None = None) -> dict[str, Any]:
    key = riot_key()
    if not key:
        raise SourceError("RIOT_API_KEY is not set on the server.", 400)
    shard = RIOT_SHARDS.get((region or DEFAULT_REGION).lower(), RIOT_SHARDS["na"])
    url = f"{shard}/val/match/v1/matchlists/by-puuid/{puuid}"
    return await _get_json(url, {"X-Riot-Token": key})
