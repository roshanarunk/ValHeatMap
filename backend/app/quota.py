"""Self-imposed quota guard for object storage.

Cloudflare has no hard spending cap -- budget alerts email you *after* the
fact but never stop anything -- so the only real protection is to not
exceed the free tier in the first place.

This tracks what we upload locally and refuses to publish when a limit
would be crossed. It guards against the realistic failure mode: a bug or a
runaway loop publishing far more often than intended. Normal operation
uses well under 1% of every limit.

R2 free tier:
    storage             10 GB
    Class A (writes)     1,000,000 / month
    Class B (reads)     10,000,000 / month

Storage is not really at risk because publishing overwrites a single
object, so it stays flat at the size of one snapshot no matter how often
we push. The limits worth enforcing are upload *size* and upload *rate*.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .paths import DATA_DIR

STATE_PATH = DATA_DIR / "quota.json"

# Refuse to upload a snapshot larger than this. The free tier is 10 GB, and
# a single snapshot this big would mean something has gone very wrong -- the
# dataset would have to reach several million matches.
MAX_UPLOAD_BYTES = 2_000_000_000  # 2 GB

# Class A operations we allow ourselves per month. The real limit is 1M; a
# budget of 10k is ~14 publishes an hour sustained, far above any sane
# schedule, while still catching a loop that publishes every second.
MAX_WRITES_PER_MONTH = 10_000

# Minimum gap between uploads. Publishing more often than this wastes
# bandwidth without making the site meaningfully fresher.
MIN_SECONDS_BETWEEN_UPLOADS = 60


class QuotaExceeded(RuntimeError):
    """Raised instead of uploading when a self-imposed limit would break."""


@dataclass
class QuotaState:
    month: str
    writes: int = 0
    bytes_uploaded: int = 0
    last_upload_ts: float = 0.0

    @classmethod
    def load(cls, path: Path | None = None) -> "QuotaState":
        target = path or STATE_PATH
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if target.exists():
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
                state = cls(
                    month=raw.get("month", month),
                    writes=int(raw.get("writes", 0)),
                    bytes_uploaded=int(raw.get("bytes_uploaded", 0)),
                    last_upload_ts=float(raw.get("last_upload_ts", 0.0)),
                )
                # A new calendar month resets the counters, matching how
                # Cloudflare bills.
                if state.month != month:
                    return cls(month=month)
                return state
            except (json.JSONDecodeError, ValueError, OSError):
                pass
        return cls(month=month)

    def save(self, path: Path | None = None) -> None:
        target = path or STATE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "month": self.month,
                    "writes": self.writes,
                    "bytes_uploaded": self.bytes_uploaded,
                    "last_upload_ts": self.last_upload_ts,
                },
                indent=1,
            ),
            encoding="utf-8",
        )

    def summary(self) -> dict[str, object]:
        return {
            "month": self.month,
            "writes": self.writes,
            "writes_budget": MAX_WRITES_PER_MONTH,
            "writes_pct": round(self.writes / MAX_WRITES_PER_MONTH * 100, 2),
            "uploaded_mb": round(self.bytes_uploaded / 1e6, 1),
            "last_upload": (
                datetime.fromtimestamp(self.last_upload_ts).strftime("%Y-%m-%d %H:%M")
                if self.last_upload_ts
                else "never"
            ),
        }


def check_upload(size_bytes: int, state: QuotaState | None = None) -> QuotaState:
    """Raise QuotaExceeded if this upload should not happen.

    Called before every PUT, so a runaway publisher is stopped at the first
    offending call rather than discovered on a bill.
    """
    state = state or QuotaState.load()

    if size_bytes > MAX_UPLOAD_BYTES:
        raise QuotaExceeded(
            f"snapshot is {size_bytes / 1e9:.2f} GB, over the "
            f"{MAX_UPLOAD_BYTES / 1e9:.0f} GB self-imposed cap. "
            "Something is wrong with the database, or the cap needs raising "
            "in app/quota.py."
        )

    if state.writes >= MAX_WRITES_PER_MONTH:
        raise QuotaExceeded(
            f"{state.writes:,} uploads already this month, at the "
            f"{MAX_WRITES_PER_MONTH:,} self-imposed budget. R2 allows 1M "
            "Class A operations free; this guard exists to catch a runaway "
            "loop. Raise MAX_WRITES_PER_MONTH if this is legitimate."
        )

    elapsed = time.time() - state.last_upload_ts
    if state.last_upload_ts and elapsed < MIN_SECONDS_BETWEEN_UPLOADS:
        raise QuotaExceeded(
            f"last upload was {elapsed:.0f}s ago; minimum gap is "
            f"{MIN_SECONDS_BETWEEN_UPLOADS}s. Publishing faster than this "
            "does not make the site meaningfully fresher."
        )

    return state


def record_upload(size_bytes: int, state: QuotaState | None = None) -> QuotaState:
    """Note a successful upload against the month's budget."""
    state = state or QuotaState.load()
    state.writes += 1
    state.bytes_uploaded += size_bytes
    state.last_upload_ts = time.time()
    state.save()
    return state


def main() -> int:
    """`python -m app.quota` -- show month-to-date usage against the budget."""
    state = QuotaState.load()
    summary = state.summary()
    print(f"Month:          {summary['month']}")
    print(f"Uploads:        {summary['writes']} / {summary['writes_budget']:,} "
          f"({summary['writes_pct']}% of self-imposed budget)")
    print(f"Data sent:      {summary['uploaded_mb']} MB")
    print(f"Last upload:    {summary['last_upload']}")
    print()
    print("R2 free tier, for reference:")
    print(f"  storage           10 GB      (we overwrite one object, so this stays flat)")
    print(f"  Class A writes     1,000,000/mo  -> our budget is {MAX_WRITES_PER_MONTH:,}")
    print(f"  Class B reads     10,000,000/mo  -> one GET per site cold start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
