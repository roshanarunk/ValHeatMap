"""How busy this machine is right now.

The crawler and the API share one shared-cpu-1x, 512MB machine, with a
database that has grown past 1.5GB on a network-attached volume, and no
swap. There is no per-request or per-batch cost that is individually too
expensive -- the problem is several cheap things landing on the same
CPU, memory headroom, or disk queue at once. Nothing here optimises a
single query; it decides when work that *can* wait, should.

Three signals, in the order they were added -- each one added because
the previous ones did not catch a real incident:

- load average (/proc/loadavg): how many processes wanted the CPU,
  averaged over 1 minute. Above 1.0 on a single vCPU means something is
  queued behind something else *right now*. Caught the facet-rebuild
  incident (load 7.99).
- MemAvailable (/proc/meminfo): the kernel's own estimate of what it
  could hand a new allocation without swapping (there is none here) or
  thrashing the page cache.
- query latency (`probe_latency`): the two signals above did not catch
  the incident where a plain `COUNT(*)` against the `kills` table took
  4.8s -- and once, 13s on a real request -- while load average and
  free memory both looked fine. That was disk I/O contention between
  the crawler's writes and API reads on the volume, which /proc does
  not expose at all. Timing an actual query against the real table is
  the only signal that measures the thing that was really slow.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# A single shared vCPU is saturated at load 1.0; treat 0.85 as already
# busy enough that new optional work should wait rather than compete for
# the last of it.
BUSY_LOAD = 0.85

# Below this, a burst allocation (a sort's temp b-tree, a bigger result
# set) has too little slack before the kernel has to reclaim page cache
# under pressure -- which is itself a multi-second stall, not a clean
# allocation.
LOW_MEM_MB = 80.0

# A cheap read against the real, large table should be single-digit
# milliseconds when the disk is not contended (measured: ~0-5ms on an
# idle volume). Above this, something -- almost always the crawler's own
# writes -- is competing for the same I/O queue.
SLOW_QUERY_S = 0.5

# Hours (in TIMEZONE) during which the crawler holds back regardless of
# how idle the machine looks by the other signals. Chosen for when real
# people are actually using the site, not when the metrics happen to be
# quiet -- the I/O-contention incident this exists for measured fine on
# load average and memory right up until a real request hung.
QUIET_HOURS = range(12, 24)  # noon .. 11pm inclusive, midnight excluded
TIMEZONE = ZoneInfo("America/New_York")


def load_average() -> float | None:
    """The 1-minute load average, or None if the platform has no support for it."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None  # not available on this platform (e.g. Windows in dev)


def available_mb() -> float | None:
    """The kernel's own estimate of allocatable memory, in MB."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None  # not Linux, or the kernel doesn't expose it


def probe_latency(db_path: str) -> float | None:
    """Time one small, real read to measure connection and seek latency.

    A fresh connection each call, deliberately: the point is to measure
    what a *new* query experiences right now, the same way a real
    request would, not to reuse a connection that might itself be primed
    or blocked in some unrepresentative way. `timeout=3` bounds the worst
    case -- if even opening the connection or running the probe takes
    that long, the answer is unambiguously "busy".
    """
    try:
        t0 = time.monotonic()
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            conn.execute("SELECT 1 FROM kills LIMIT 1").fetchone()
        finally:
            conn.close()
        return time.monotonic() - t0
    except sqlite3.Error:
        return None  # can't tell; do not let a probe failure block real work


def in_quiet_hours(now: datetime | None = None) -> bool:
    """Is it currently a time real people are likely using the site?

    The crawler throttles harder during these hours regardless of what
    the load/memory/latency probes say, because the incident this exists
    for was invisible to all three until a real request actually hung --
    a fixed quiet window is a floor under the reactive signals, not a
    replacement for them.
    """
    moment = (now or datetime.now(TIMEZONE)).astimezone(TIMEZONE)
    return moment.hour in QUIET_HOURS


def is_busy(
    db_path: str | None = None,
    load_ceiling: float = BUSY_LOAD,
    mem_floor: float = LOW_MEM_MB,
    latency_ceiling: float = SLOW_QUERY_S,
) -> bool:
    """Should optional, deferrable work wait rather than run right now?

    Errs toward "no" when a signal is unavailable (e.g. running locally on
    Windows, where getloadavg and /proc do not exist) -- backoff logic
    that always reports "not busy" on a platform it cannot read is safer
    than one that always defers. `db_path` is optional for the same
    reason: callers that have not built the database path yet still get
    a usable answer from the other two signals.
    """
    load = load_average()
    if load is not None and load >= load_ceiling:
        return True
    mem = available_mb()
    if mem is not None and mem <= mem_floor:
        return True
    if db_path is not None:
        latency = probe_latency(db_path)
        if latency is not None and latency >= latency_ceiling:
            return True
    return False
