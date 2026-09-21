"""Vercel entrypoint.

Vercel looks for a top-level `app` in this file and serves it as an ASGI
function. The heavy lifting stays in `backend/app`; this only puts that
package on the path and marks the deployment read-only, since a serverless
filesystem cannot accept crawler writes.

The analytics database is not bundled -- it is fetched from object storage
on cold start (see `backend/app/snapshot.py`), because at ~100 MB and
growing it would otherwise bloat every deployment.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

# A Vercel function's filesystem is read-only apart from /tmp.
os.environ.setdefault("VALHEATMAP_READ_ONLY", "1")
os.environ.setdefault("VALHEATMAP_CACHE_DIR", "/tmp")

from app.main import app  # noqa: E402

__all__ = ["app"]
