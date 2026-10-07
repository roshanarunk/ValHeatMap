#!/usr/bin/env bash
# Nightly backup of ValHeatMap's databases on Hetzner.
# Uses SQLite's online backup API, so it never locks or corrupts live queries.
#
# Recommended cron setup:
#   0 4 * * * /opt/valheatmap/deploy/backup.sh >> /var/log/valheatmap-backup.log 2>&1
#
# These copies live on the same disk as the database, so they protect
# against a bad write or a bad migration, not against losing the server.
# For that, enable Hetzner's Backups add-on on the server as well.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/valheatmap}"
DATA_DIR="$APP_DIR/data"
BACKUP_DIR="$APP_DIR/backups"
# Count, not age: each copy is several GB, so a fixed number is what
# keeps the disk from filling. At ~14 GB of database, 3 copies take ~20 GB.
KEEP="${KEEP:-3}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

echo "==> [$(date)] Starting ValHeatMap database backup"

# The snapshot is written uncompressed first, then gzipped: room for the
# database plus about half again is needed while that happens. Running
# short would fill the disk under the live database, so skip instead.
db_bytes=$(stat -c %s "$DATA_DIR/analytics.db")
free_bytes=$(df --output=avail -B1 "$DATA_DIR" | tail -1)
need_bytes=$(( db_bytes * 3 / 2 ))
if [ "$free_bytes" -lt "$need_bytes" ]; then
    echo "!! Skipping backup: $((free_bytes / 1000000000)) GB free, ~$((need_bytes / 1000000000)) GB needed." >&2
    exit 1
fi

if docker ps --format '{{.Names}}' | grep -q "^valheatmap-app$"; then
    echo "Backing up via Docker container valheatmap-app..."
    docker exec valheatmap-app python -c "
import sqlite3
for name in ('analytics.db', 'valheatmap.db'):
    src = '/data/' + name
    dst = '/data/' + name + '.bak'
    try:
        s = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
        d = sqlite3.connect(dst)
        s.backup(d)
        d.close()
        s.close()
    except Exception as e:
        print(f'Warning: {name} backup skipped: {e}')
"
    mv "$DATA_DIR/analytics.db.bak" "$BACKUP_DIR/analytics_${TIMESTAMP}.db" 2>/dev/null || true
    mv "$DATA_DIR/valheatmap.db.bak" "$BACKUP_DIR/valheatmap_${TIMESTAMP}.db" 2>/dev/null || true
elif command -v sqlite3 >/dev/null 2>&1; then
    echo "Backing up via host sqlite3..."
    sqlite3 "$DATA_DIR/analytics.db" ".backup '$BACKUP_DIR/analytics_${TIMESTAMP}.db'"
    if [ -f "$DATA_DIR/valheatmap.db" ]; then
        sqlite3 "$DATA_DIR/valheatmap.db" ".backup '$BACKUP_DIR/valheatmap_${TIMESTAMP}.db'"
    fi
else
    echo "!! Neither the app container nor sqlite3 is available; nothing backed up." >&2
    exit 1
fi

for name in analytics valheatmap; do
    if [ -f "$BACKUP_DIR/${name}_${TIMESTAMP}.db" ]; then
        gzip -f "$BACKUP_DIR/${name}_${TIMESTAMP}.db"
        echo "Backup completed: $BACKUP_DIR/${name}_${TIMESTAMP}.db.gz"
    fi
done

echo "Keeping the newest $KEEP backups of each database..."
for name in analytics valheatmap; do
    ls -1t "$BACKUP_DIR/${name}_"*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
done

echo "==> [$(date)] Backup finished successfully"
