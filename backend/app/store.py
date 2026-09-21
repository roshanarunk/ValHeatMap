"""Match loading, caching and cross-match aggregation.

Matches from every source land in one in-memory index keyed by match id.
Because all analytics run on the canonical model, a heatmap can span a
locally bundled match, one pulled from HenrikDev and one uploaded by hand.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import Match
from .paths import DATA_DIR as _DATA_DIR
from .sources import henrik, riot

DATA_DIR = _DATA_DIR / "matches"


def parse_any(data: dict[str, Any], source: str | None = None) -> Match:
    """Detect the payload's shape and dispatch to the right adapter."""
    if henrik.looks_like(data):
        return henrik.parse(data, source=source or "henrik")
    if riot.looks_like(data):
        return riot.parse(data, source=source or "riot")
    raise ValueError("Unrecognised match payload: expected Riot match-v1 or HenrikDev v4 shape")


class MatchStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or DATA_DIR
        self._matches: dict[str, Match] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # --- loading -------------------------------------------------------
    def _load_file(self, path: Path, source: str) -> bool:
        try:
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
            match = parse_any(payload, source=source)
            if not match.meta.match_id:
                # Fall back to the filename so bundled samples without a
                # match id still get a stable, linkable key.
                match.meta.match_id = path.stem
            self.add(match)
            return True
        except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
            print(f"[store] skipping {path.name}: {exc}")
            return False

    def load_local(self) -> int:
        """Ingest bundled sample matches plus everything the crawler stored.

        Both live in the same in-memory index, so an analysis can span a
        bundled match and thousands of crawled ones.
        """
        count = 0
        if self.data_dir.exists():
            for path in sorted(self.data_dir.glob("*.json")):
                count += self._load_file(path, "local")

        # Crawled matches: indexed in SQLite, payloads on disk.
        try:
            from .db import db as match_db

            for path in match_db.iter_payload_paths():
                if path.exists():
                    count += self._load_file(path, "henrik")
        except Exception as exc:  # pragma: no cover - DB is optional
            print(f"[store] crawled matches unavailable: {exc}")

        self._loaded = True
        return count

    def reload(self) -> int:
        """Re-read everything from disk, picking up new crawler output."""
        with self._lock:
            self._matches.clear()
            self._loaded = False
        return self.load_local()

    def ensure_loaded(self) -> None:
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self.load_local()

    def add(self, match: Match) -> Match:
        with self._lock:
            self._matches[match.meta.match_id] = match
        return match

    # --- access --------------------------------------------------------
    def get(self, match_id: str) -> Match | None:
        self.ensure_loaded()
        return self._matches.get(match_id)

    def all(self) -> list[Match]:
        self.ensure_loaded()
        return sorted(self._matches.values(), key=lambda m: m.meta.started_at, reverse=True)

    def select(
        self,
        match_ids: Iterable[str] | None = None,
        map_name: str | None = None,
        mode: str | None = None,
    ) -> list[Match]:
        """Pick matches for an aggregate query."""
        self.ensure_loaded()
        ids = {m for m in (match_ids or ()) if m}
        out = []
        for match in self.all():
            if ids and match.meta.match_id not in ids:
                continue
            if map_name and match.meta.map_name.lower() != map_name.lower():
                continue
            if mode and match.meta.mode != mode:
                continue
            out.append(match)
        return out

    def maps(self) -> list[dict[str, Any]]:
        """Maps present in the store, with match/kill counts."""
        agg: dict[str, dict[str, Any]] = {}
        for match in self.all():
            row = agg.setdefault(
                match.meta.map_name,
                {
                    "map_name": match.meta.map_name,
                    "map_id": match.meta.map_id,
                    "matches": 0,
                    "kills": 0,
                    "plants": 0,
                    "modes": set(),
                },
            )
            row["matches"] += 1
            row["kills"] += len(match.kills)
            row["plants"] += len(match.plants)
            row["modes"].add(match.meta.mode)
        out = []
        for row in agg.values():
            row["modes"] = sorted(row["modes"])
            out.append(row)
        # Richest map first: it makes the best default selection, and a map
        # with one 1v1 custom shouldn't outrank a full competitive match.
        out.sort(key=lambda r: (-r["kills"], -r["matches"], r["map_name"]))
        return out


store = MatchStore()
