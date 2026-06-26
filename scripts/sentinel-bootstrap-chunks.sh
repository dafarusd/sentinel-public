#!/bin/bash
# sentinel-bootstrap-chunks.sh
# ONE-TIME script: converts the existing sentinel.db into sentinel-001.db
# and creates a fresh empty sentinel.db.
#
# Per spec 2A: the current 290MB DB becomes chunk 1 immediately.
# Run this ONCE, while Sentinel is stopped, BEFORE deploying the rotator.

set -euo pipefail

DATA_DIR="/mnt/ssd/sentinel-data"
DB="${DATA_DIR}/sentinel.db"
CHUNKS_DIR="${DATA_DIR}/chunks"
SCHEMA="/home/user/sentinel/schema.sql"

# ---- Safety checks ----
[ -f "$DB" ] || { echo "ERROR: DB not found: $DB" >&2; exit 1; }
[ -f "$SCHEMA" ] || { echo "ERROR: Schema not found: $SCHEMA" >&2; exit 1; }

# Refuse to run if Sentinel is active (DB might be locked)
if pgrep -f "sentinel-(wifi|bt|ingest|detector|profiler)" >/dev/null 2>&1; then
    echo "ERROR: Sentinel processes are running. Stop Sentinel first:" >&2
    echo "  cd ~/sentinel && ./sentinel.sh stop" >&2
    exit 1
fi

# Refuse to run if chunks already exist
if [ -d "$CHUNKS_DIR" ] && [ "$(ls -A "$CHUNKS_DIR"/sentinel-*.db 2>/dev/null)" ]; then
    echo "ERROR: chunks already exist in $CHUNKS_DIR - bootstrap already done" >&2
    echo "  Contents:" >&2
    ls -la "$CHUNKS_DIR" >&2
    exit 1
fi

mkdir -p "$CHUNKS_DIR"

echo "=== Sentinel chunk bootstrap ==="
echo "  Current DB: ${DB}"
echo "  Size: $(du -h "$DB" | awk '{print $1}')"
echo "  Target: ${CHUNKS_DIR}/sentinel-001.db"
echo ""
read -p "Proceed? (yes/no) " answer
if [ "$answer" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

# ---- WAL checkpoint so DB is self-contained ----
echo "Checkpointing WAL..."
sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);"

# ---- Gather metadata BEFORE moving ----
echo "Computing metadata..."
STARTED_AT=$(sqlite3 "$DB" "SELECT MIN(timestamp) FROM observations" 2>/dev/null || echo "unknown")
SEALED_AT=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
ROW_OBS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM observations")
ROW_WIFI=$(sqlite3 "$DB" "SELECT COUNT(*) FROM wifi_frames")
ROW_PROBE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM probe_requests")
ROW_BT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM bt_advertisements")
ROW_ALERT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM alerts")
ROW_DEV=$(sqlite3 "$DB" "SELECT COUNT(*) FROM devices")
UNIQUE_SSIDS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT ssid) FROM probe_requests WHERE ssid != ''" 2>/dev/null || echo 0)

# ---- Move the file ----
NEXT_DB="${CHUNKS_DIR}/sentinel-001.db"
NEXT_META="${CHUNKS_DIR}/sentinel-001.meta.json"

echo "Moving to chunk location..."
mv "$DB" "$NEXT_DB"
# Clean up stale sidecars
rm -f "${DB}-wal" "${DB}-shm"

# Compute checksum
CHECKSUM=$(sha256sum "$NEXT_DB" | awk '{print $1}')
SEALED_SIZE=$(stat -c %s "$NEXT_DB")

# ---- Write meta JSON ----
cat > "$NEXT_META" <<EOF
{
  "chunk_id": 1,
  "filename": "sentinel-001.db",
  "size_bytes": ${SEALED_SIZE},
  "size_mb": $(( SEALED_SIZE / 1024 / 1024 )),
  "started_at": "${STARTED_AT}",
  "sealed_at": "${SEALED_AT}",
  "row_counts": {
    "devices": ${ROW_DEV},
    "observations": ${ROW_OBS},
    "wifi_frames": ${ROW_WIFI},
    "probe_requests": ${ROW_PROBE},
    "bt_advertisements": ${ROW_BT},
    "alerts": ${ROW_ALERT}
  },
  "unique_ssids_probed": ${UNIQUE_SSIDS},
  "sha256": "${CHECKSUM}",
  "note": "Bootstrap chunk - sealed at rotator deployment"
}
EOF

echo "Sealed: sentinel-001.db"
echo "  Size: $(du -h "$NEXT_DB" | awk '{print $1}')"
echo "  Observations: ${ROW_OBS}"
echo "  Devices: ${ROW_DEV}"

# ---- Create fresh DB ----
echo ""
echo "Creating fresh sentinel.db..."
sqlite3 "$DB" "PRAGMA journal_mode=WAL;"
sqlite3 "$DB" < "$SCHEMA"

# Carry forward devices + oui
echo "Carrying forward devices and OUI data..."
sqlite3 "$DB" <<SQL
ATTACH DATABASE '${NEXT_DB}' AS prev;
INSERT INTO devices SELECT * FROM prev.devices;
INSERT INTO oui_vendors SELECT * FROM prev.oui_vendors;
DETACH DATABASE prev;
SQL

DEV_CARRIED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM devices")
OUI_CARRIED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM oui_vendors")

chown kali:kali "$DB"

echo ""
echo "=== Bootstrap complete ==="
echo "  Fresh DB:          ${DB}"
echo "  Fresh DB size:     $(du -h "$DB" | awk '{print $1}')"
echo "  Devices carried:   ${DEV_CARRIED} (including all your annotations)"
echo "  OUI entries:       ${OUI_CARRIED}"
echo ""
echo "Next steps:"
echo "  1. Install rotator: sudo cp sentinel-rotate.sh /home/user/sentinel/scripts/"
echo "  2. Install units:   sudo cp sentinel-rotator.service sentinel-rotator.timer /etc/systemd/system/"
echo "  3. Enable timer:    sudo systemctl daemon-reload && sudo systemctl enable --now sentinel-rotator.timer"
echo "  4. Start Sentinel:  cd ~/sentinel && ./sentinel.sh start"
