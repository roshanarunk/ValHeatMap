#!/bin/sh
# Run the API and the crawler in one container.
#
# They share /data: the crawler writes the database, the API reads it.
# That is the whole point of moving off serverless -- there is no snapshot
# to publish and no size ceiling to stay under.
set -eu

DATA_DIR="${VALHEATMAP_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

# Run schema/index migration once, before either process starts, rather
# than letting both construct AnalyticsDB() at the same moment. On every
# boot -- not just the first -- AnalyticsDB.__init__ runs the full SCHEMA
# script, including any CREATE INDEX IF NOT EXISTS added for a new
# migration. Verifying "IF NOT EXISTS" or building the index over a
# multi-million-row table takes real time under real disk contention, and
# if the crawler and the API both do that unserialized on the same boot,
# one holds SQLite's write lock past the other's connection timeout: seen
# in production as `sqlite3.OperationalError: database is locked` in the
# API's startup path, immediately after it, which exhausted the machine's
# restart budget. Paying that cost once, synchronously, here, means
# neither backgrounded process ever has to pay it again this boot.
echo "[start] ensuring schema and indexes are current"
python -c "from app.analytics_db import AnalyticsDB; AnalyticsDB()"

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
