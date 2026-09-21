"""Where the application keeps its data.

Local runs use `data/` beside the repo. A container mounts a volume and
sets VALHEATMAP_DATA_DIR, so the database survives deploys and is shared
between the API and the crawler.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(
    os.environ.get("VALHEATMAP_DATA_DIR")
    or Path(__file__).resolve().parents[2] / "data"
)


def data_path(*parts: str) -> Path:
    """A path inside the data directory, creating the directory if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR.joinpath(*parts)
