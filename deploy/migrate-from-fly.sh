#!/usr/bin/env bash
# Migrate production data from Fly.io to Hetzner (Bash version).
#
# Usage:
#   ./deploy/migrate-from-fly.sh [HETZNER_HOST] [HETZNER_DATA_DIR]
#
# Example:
#   ./deploy/migrate-from-fly.sh root@192.0.2.1 /opt/valheatmap/data

set -euo pipefail

APP="${FLY_APP:-valheatmap}"
HETZNER_HOST="${1:-}"
HETZNER_DATA_DIR="${2:-/opt/valheatmap/data}"
LOCAL_ARCHIVE="./data/valheatmap-migration.tar.gz"

echo "=========================================================="
echo "      ValHeatMap Migration: Fly.io -> Hetzner Cloud      "
echo "=========================================================="

if ! command -v fly >/dev/null 2>&1; then
    echo "Error: fly CLI not found in PATH." >&2
    exit 1
fi

echo "==> 1. Flushing SQLite WAL logs on Fly.io..."
fly ssh console --app "$APP" -C "python3 -c '
import sqlite3
for db in (\"/data/analytics.db\", \"/data/valheatmap.db\"):
    try:
        c = sqlite3.connect(db)
        c.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\")
        c.close()
        print(f\"Checkpointed {db}\")
    except Exception as e:
        print(f\"Error: {e}\")
'"

echo "==> 2. Pausing Fly.io crawler..."
fly secrets set VALHEATMAP_CRAWLER=0 --app "$APP" >/dev/null 2>&1 || true
sleep 3

echo "==> 3. Creating compressed archive on Fly.io..."
fly ssh console --app "$APP" -C "cd /data && tar -czf /data/migration.tar.gz analytics.db valheatmap.db raw"

mkdir -p ./data
rm -f "$LOCAL_ARCHIVE"

echo "==> 4. Downloading archive..."
fly ssh sftp get /data/migration.tar.gz "$LOCAL_ARCHIVE" --app "$APP"

echo "==> 5. Cleaning up archive on Fly.io..."
fly ssh console --app "$APP" -C "rm -f /data/migration.tar.gz" >/dev/null 2>&1 || true

if [[ -n "$HETZNER_HOST" ]]; then
    echo "==> 6. Transferring archive to $HETZNER_HOST..."
    ssh "$HETZNER_HOST" "mkdir -p '$HETZNER_DATA_DIR'"
    scp "$LOCAL_ARCHIVE" "${HETZNER_HOST}:${HETZNER_DATA_DIR}/valheatmap-migration.tar.gz"

    echo "==> 7. Unpacking on Hetzner host..."
    ssh "$HETZNER_HOST" "cd '$HETZNER_DATA_DIR' && tar -xzf valheatmap-migration.tar.gz && rm -f valheatmap-migration.tar.gz && ls -lah '$HETZNER_DATA_DIR'"
    echo "==> Migration complete!"
else
    echo "Archive downloaded to $LOCAL_ARCHIVE"
    echo "To upload to Hetzner manually:"
    echo "  scp $LOCAL_ARCHIVE root@<HETZNER_IP>:$HETZNER_DATA_DIR/"
    echo "  ssh root@<HETZNER_IP> 'cd $HETZNER_DATA_DIR && tar -xzf valheatmap-migration.tar.gz && rm valheatmap-migration.tar.gz'"
fi
