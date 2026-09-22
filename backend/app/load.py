"""How busy this machine is right now.

The crawler and the API share one shared-cpu-1x, 512MB machine with no
swap. There is no per-request or per-batch cost that is individually too
expensive -- the problem is several cheap things landing on the same
core and the same few hundred MB of headroom at once, most often the
crawler's write batch, a facet-cache rebuild, and a burst of My Stats
page requests. Nothing here optimises a single query; it decides when
work that *can* wait, should.

Both signals come straight from /proc, which every Linux container
exposes with no extra dependency:

- load average: how many processes wanted the CPU, averaged over 1
  minute. Above 1.0 on a single vCPU means something is queued behind
  something else *right now*.
- MemAvailable: the kernel's own estimate of what it could hand a new
  allocation without swapping (there is none here) or thrashing the page
  cache. Cheaper and more honest than MemFree, which does not count
  reclaimable cache.
"""

from __future__ import annotations

import os

# A single shared vCPU is saturated at load 1.0; treat 0.85 as already
# busy enough that new optional work should wait rather than compete for
# the last of it.
BUSY_LOAD = 0.85

# Below this, a burst allocation (a sort's temp b-tree, a bigger result
# set) has too little slack before the kernel has to reclaim page cache
# under pressure -- which is itself a multi-second stall, not a clean
# allocation.
LOW_MEM_MB = 80.0


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


def is_busy(load_ceiling: float = BUSY_LOAD, mem_floor: float = LOW_MEM_MB) -> bool:
    """Should optional, deferrable work wait rather than run right now?

    Errs toward "no" when a signal is unavailable (e.g. running locally on
    Windows, where getloadavg and /proc do not exist) -- backoff logic
    that always reports "not busy" on a platform it cannot read is safer
    than one that always defers.
    """
    load = load_average()
    if load is not None and load >= load_ceiling:
        return True
    mem = available_mb()
    if mem is not None and mem <= mem_floor:
        return True
    return False
