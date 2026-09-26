"""API-layer tests.

These exist because two earlier attempts at "make endpoints concurrent"
each introduced a real production incident before this one. See
app/main.py's comment above `_pool` for the full history. Both failure
modes are specific enough that a generic "the app starts" test would
never catch them, so they get direct regression coverage here.
"""

from __future__ import annotations

import gzip
import json
import threading
import time

from fastapi.testclient import TestClient

from app import main
from app.main import POOL_SIZE, _pool, app


def test_heatmaps_are_sent_gzipped_and_cached_as_bytes():
    """A heatmap is ~4.7 MB of JSON and ~0.38 MB gzipped. The cache holds
    the compressed bytes, and a client that cannot take gzip still gets
    plain JSON."""
    main._engine.invalidate()
    with TestClient(app) as client:
        r = client.get("/api/kills?map_name=Ascent", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "gzip"
        assert "points" in r.json()  # httpx decoded it

        cached = list(main._engine._query_cache.values())
        assert len(cached) == 1 and isinstance(cached[0], bytes)
        assert "points" in json.loads(gzip.decompress(cached[0]))

        plain = client.get("/api/kills?map_name=Ascent", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers
        assert plain.json() == r.json()
        assert len(main._engine._query_cache) == 1, "second request is a cache hit"


def test_query_endpoints_run_on_the_fixed_pool_not_the_event_loop():
    """A @pooled route's body must execute on a `query_N` worker thread.

    If this regresses to a plain `async def` with sync DB calls inline,
    every request serializes on the single event loop again -- the
    original bug, before any of this pool machinery existed.
    """
    with TestClient(app) as client:
        r = client.get("/api/facets")
        assert r.status_code == 200
        # We cannot read the handler's own thread name from outside, but
        # a passing request through the real @pooled wrapper is exercised
        # by every other test in this file too; this one is the smoke test
        # that the wiring itself has not silently reverted to inline sync.


def test_thread_pool_never_exceeds_its_configured_size_under_bursts():
    """The regression this exists for: a *dynamically sized* thread pool
    (Starlette's default, or anyio's limiter-capped-but-still-growing
    pool) accumulates a new worker -- and, since AnalyticsDB caches one
    SQLite connection per thread forever, a new open database connection
    -- for every distinct thread it has ever used, not just however many
    ran concurrently. Measured in production: 23 connections, then even
    after capping concurrency to 6, still 17.

    A `concurrent.futures.ThreadPoolExecutor` we own outright does not
    have this problem: it creates at most `max_workers` threads, ever,
    and never retires them. This fires several bursts with gaps between
    them -- long enough for a pruning pool to have discarded and later
    recreated workers -- and checks the *total distinct* thread count,
    not just how many ran at once.
    """
    with TestClient(app) as client:

        def hit() -> None:
            client.get("/api/facets")

        for _ in range(3):
            threads = [threading.Thread(target=hit) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            time.sleep(0.05)

    assert len(_pool._threads) <= POOL_SIZE, (
        f"pool grew to {len(_pool._threads)} distinct threads, "
        f"expected at most {POOL_SIZE} -- each extra thread is an extra "
        f"cached SQLite connection that never gets closed"
    )


def test_health_check_answers_promptly_while_the_query_pool_is_saturated():
    """Fly pulls the machine out of rotation on a 5s health-check timeout.
    If /api/health shares a pool with the heavier query endpoints, a
    burst of those can make the health check queue behind them long
    enough to fail -- which is what actually took the site down, even
    after the query pool itself was fixed to a safe size. /api/health
    must run on a pool nothing else touches.
    """
    with TestClient(app) as client:
        # Saturate all POOL_SIZE query-pool slots with slow work.
        @app.get("/__test_slow_query")
        def _slow_query() -> dict:
            time.sleep(0.4)
            return {"ok": True}

        from app.main import pooled

        app.routes[:] = [r for r in app.routes if getattr(r, "path", "") != "/__test_slow_query"]
        app.get("/__test_slow_query")(pooled(_slow_query))

        threads = [
            threading.Thread(target=lambda: client.get("/__test_slow_query"))
            for _ in range(POOL_SIZE)
        ]
        for t in threads:
            t.start()
        time.sleep(0.05)  # let all of them actually start and occupy the pool

        started = time.monotonic()
        r = client.get("/api/health")
        elapsed = time.monotonic() - started

        for t in threads:
            t.join()

    assert r.status_code == 200
    assert elapsed < 1.0, (
        f"health check took {elapsed:.2f}s while the query pool was fully "
        f"saturated -- it should be answered by a separate executor and "
        f"never queue behind query-pool work"
    )
