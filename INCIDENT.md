# ValHeatMap 503s: what's happening, and what I tried

Written for: Roshan, to review the diagnosis and the fixes before deciding
whether to also scale the machine.

## The short version

The site has hit **two distinct bugs** and **one real capacity limit**,
all producing the same symptom (503s / hangs, worst on My Stats). Two of
the three are fixed and verified. The third — disk I/O contention between
the crawler's writes and the API's reads on a 1.5GB+ database, on a
single shared vCPU with no dedicated I/O bandwidth — is a genuine
hardware ceiling that a thread-pool or index fix cannot remove. What's
shipped now (query-latency-aware crawler backoff + a quiet-hours throttle)
reduces how often the crawler triggers it; it doesn't remove the ceiling
itself.

## Timeline of what was actually wrong, in the order I found it

### 1. Facet cache rebuild pegged the CPU (fixed)

`_compute_facets()` grouped the whole `kills` table (millions of rows) by
agent, weapon, and ability with no supporting index, so each was a full
table scan plus a temp B-tree. Measured: **139 seconds**, run every 400
new matches. On a *shared* vCPU, that's not "139 seconds of extra
latency" — it's 139 seconds where the crawler process (which shares the
one core with the API process) can starve the API's health check.

**Fix:** added `idx_k_agent`, `idx_k_weapon`, `idx_k_ability`. Verified
with `EXPLAIN QUERY PLAN` before/after, and end-to-end: 139s → 0.43s at
the time. This held up — it's not what's causing the current incidents.

### 2. Every read-only route ran on the single event loop (fixed)

Separately, every query endpoint (`/api/kills`, `/api/facets`,
`/api/player/...`, even `/api/health`) was `async def` with no actual
`await` inside. FastAPI runs those directly on the event loop, so every
request was **fully serialized** — one request had to finish before the
next one could even start, health check included. A burst of My Stats
filter changes (each firing 2+ requests) queued up on that one loop.

**Fix, attempt 1:** converted the handlers to plain `def`, which lets
Starlette run them on its own thread pool automatically. This worked for
concurrency, but:

**Fix, attempt 1 caused a new bug:** Starlette's thread pool is dynamic —
it creates a new persistent worker whenever none are idle, and only
prunes idle ones after 10 seconds. `AnalyticsDB` caches one SQLite
connection **per thread, forever** (`threading.local()`). So the
concurrency cap I set (`anyio`'s limiter, capped to 6) bounded how many
requests ran *at once*, but not how many distinct threads — and thus
distinct open database connections — got created over the process's
life. Confirmed directly on production: **23 open connections** to the
database file from one API process after a burst of traffic, RSS up from
~104MB to ~172MB, load average **7.99** on the one vCPU.

**Fix, attempt 2:** capped the limiter to 6 — reduced the damage (load
7.99 → 1.82, RSS 172MB → 117MB) but production still showed **17** open
connections and the health check was still failing sometimes. The
concurrency cap doesn't stop *new* threads being spun up over time as
old idle ones get pruned and traffic keeps coming in bursts with gaps —
each new thread is still a new, never-closed connection.

**Fix, attempt 3 (the one that's actually correct):** stopped relying on
Starlette/anyio's dynamic pool entirely. Replaced it with a
`concurrent.futures.ThreadPoolExecutor` the app owns outright — exactly
6 worker threads, created once, **never** retired. Every query endpoint
now goes through a `@pooled` decorator that runs it on this fixed pool.
Verified directly: replayed the exact bursty-traffic pattern (96
requests across 6 bursts with gaps) against the real app and confirmed
the thread count stayed at exactly 6 the whole time. `/api/health` uses
a *separate* pool (asyncio's own default executor) so it can never queue
behind the heavier query pool — verified by saturating the query pool
for 1.5s and confirming health still answers in under a second.

This part is genuinely fixed. It's in the codebase with regression tests
(`test_main.py`) that assert on the exact properties above, not just "the
app starts".

### 3. Disk I/O contention (not fixed — mitigated)

After #1 and #2 were both deployed and verified, the site *still* went
down, and you reported it specifically while filtering by weapon on My
Stats, and separately that filters "don't affect stats anymore" (which
turned out to be the same thing — the request was hanging, and by the
time it finally returned, it looked like nothing had happened).

I chased this for a while down the wrong path first — re-checked the
connection-pool fix (still holding, confirmed 13 stable connections, not
climbing), checked for a stuck WAL checkpoint (found one: WAL had grown
to **178MB** with an incomplete checkpoint; forced a manual checkpoint
that dropped it to 0 bytes instantly) — and the request *still* hung
immediately after that, which ruled WAL bloat out as the cause of that
specific hang (it's still worth having fixed, but it wasn't this).

What actually explained it: I ran a bare `COUNT(*) FROM kills WHERE
map_id=1` directly against the live production database file, no app
code involved at all, from an SSH session on the machine itself.

```
matches table (small):  0.2s
kills table (large):    2.6s – 4.8s
```

That's not a query-plan problem (both queries use an index) and it's not
a thread-pool problem (this bypassed the app entirely) — it's the disk
itself. Fly's volumes are network-attached storage, not local SSD; a
1.5GB+ database with the crawler continuously writing (200+ matches a
batch, INSERTing into the same large table a read is trying to scan)
means reads and writes are queuing for the same limited I/O, and once
that queue backs up, everything downstream — the query, the request, the
health check — waits behind it. Restarting the machine fixed
`/api/health` immediately (nothing to catch up on) but did **not** fix
this, because it's not a leaked resource — it's genuinely contended disk
bandwidth, happening again on a freshly restarted process.

**Why the earlier fixes didn't catch this:** `load.py`'s two signals
(1-minute load average, free memory) both looked fine during this — the
CPU wasn't pegged and there was plenty of RAM. Neither of those measures
disk I/O latency at all.

## What's shipped now for #3

1. **A third signal in `load.py`**: `probe_latency()` runs a real,
   small `COUNT(*)` against the `kills` table and times it. If it's
   slow, the disk is contended right now, regardless of what CPU/memory
   say. `is_busy()` now checks this too.
2. **A blanket quiet-hours throttle**, independent of the reactive
   checks: the crawler pauses 60s (vs. the reactive 15s) between every
   batch, and defers the facet-cache rebuild outright, from **noon to
   midnight, US Eastern**, every day — not just when it happens to
   detect trouble. You asked for this specifically because the reactive
   signals had already been shown to look "fine" right up until a real
   request hung; a fixed floor doesn't have that blind spot.

This **reduces how often** the crawler and API's I/O collide during the
hours people are actually using the site. It does not remove the
ceiling: if enough real traffic hits My Stats during those hours anyway,
or the crawler's own reads (matchlist lookups, etc.) contend with the
API on their own, the same slowdown can still happen — just less often,
and the crawler is doing its part to stay out of the way rather than
making it worse.

## What would actually remove the ceiling (not done — your call)

- A bigger machine / dedicated CPU tier, so the crawler and API aren't
  sharing one vCPU's worth of everything, including I/O scheduling
  priority.
- A volume backed by faster storage, if Fly offers one, so the same I/O
  pattern simply completes faster.
- Splitting the crawler onto its own machine, writing to the same
  volume, so its write load never competes with the API's reads on the
  same process's scheduling at all (though they'd still share the
  volume's actual disk bandwidth).

I raised the "scale up" option earlier in this conversation and you chose
to reduce load on the current machine instead, which is what's shipped.
Flagging it again now that the specific bottleneck (I/O, not CPU) is
confirmed, in case it changes the calculus — happy to leave it as-is too.

## Everything mentioned above, verified how

- Facet query plans: `EXPLAIN QUERY PLAN`, before/after the new indexes.
- Thread pool bound: fired 96 requests across 6 bursts with gaps against
  the real app, read back `len(_pool._threads)`, confirmed it never
  exceeded 6.
- Health check isolation: saturated all 6 query-pool slots with 1.5s
  work, confirmed `/api/health` still answered in under a second.
- Disk contention: timed the same query against `matches` (small) and
  `kills` (large) on the live production file, directly, no app code.
- WAL bloat: measured the `.db-wal` file size before and after a manual
  `PRAGMA wal_checkpoint(TRUNCATE)`.
- Quiet-hours boundary and timezone conversion: unit tests in
  `test_load.py` checking every hour 0–23 and the exact noon/midnight
  edges, plus a UTC-input case to catch a naive "assume it's already ET"
  bug.

162 backend tests pass as of this write-up.
