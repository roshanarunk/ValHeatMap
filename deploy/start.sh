#!/bin/sh
# Run the API and the crawler in one container.
#
# They share /data: the crawler writes the database, the API reads it.
# That is the whole point of moving off serverless -- there is no snapshot
# to publish and no size ceiling to stay under.
set -eu

DATA_DIR="${VALHEATMAP_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

# First boot on an empty volume: create the schema so the API can answer
# immediately rather than erroring until the crawler's first batch lands.
if [ ! -f "$DATA_DIR/analytics.db" ]; then
  echo "[start] no database on the volume; creating an empty one"
  python -c "from app.analytics_db import AnalyticsDB; AnalyticsDB()"
fi

# The API is the process Fly health-checks, so it runs in the foreground
# and the crawler runs beside it.
if [ "${VALHEATMAP_CRAWLER:-1}" = "1" ]; then
  echo "[start] starting crawler"
  python -m app.crawler --forever --batch 200 --publish-every 0 &
  CRAWLER_PID=$!
  # If the crawler dies, log it but keep serving: a stale site beats no
  # site, and Fly restarting the machine would not fix a crawler bug.
  ( wait $CRAWLER_PID; echo "[start] crawler exited with $?" ) &
fi

echo "[start] starting API"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
