#!/usr/bin/env bash
# Nightly atomic backup script for ValHeatMap on Hetzner.
# Uses SQLite's online backup API so backups never lock or corrupt live queries.
#
# Recommended cron setup:
#   0 4 * * * /opt/valheatmap/deploy/backup.sh >> /var/log/valheatmap-backup.log 2>&1

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/valheatmap}"
DATA_DIR="$APP_DIR/data"
BACKUP_DIR="$APP_DIR/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

echo "==> [$(date)] Starting ValHeatMap database backup"

# If docker is running the app, execute the safe python backup inside the container
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
else
    # Fallback to host sqlite3 or python
    if command -v sqlite3 >/dev/null 2>&1; then
        echo "Backing up via host sqlite3..."
        sqlite3 "$DATA_DIR/analytics.db" ".backup '$BACKUP_DIR/analytics_${TIMESTAMP}.db'"
        if [ -f "$DATA_DIR/valheatmap.db" ]; then
            sqlite3 "$DATA_DIR/valheatmap.db" ".backup '$BACKUP_DIR/valheatmap_${TIMESTAMP}.db'"
        fi
    fi
fi

# Compress the backup
if [ -f "$BACKUP_DIR/analytics_${TIMESTAMP}.db" ]; then
    gzip -f "$BACKUP_DIR/analytics_${TIMESTAMP}.db"
    echo "Backup completed: $BACKUP_DIR/analytics_${TIMESTAMP}.db.gz"
fi
if [ -f "$BACKUP_DIR/valheatmap_${TIMESTAMP}.db" ]; then
    gzip -f "$BACKUP_DIR/valheatmap_${TIMESTAMP}.db"
fi

# Rotate backups: retain only the last 7 daily archives
echo "Cleaning up backups older than 7 days..."
find "$BACKUP_DIR" -type f -name "*.db.gz" -mtime +7 -delete

echo "==> [$(date)] Backup finished successfully"
