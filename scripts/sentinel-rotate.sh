#!/bin/bash
# sentinel-rotate.sh
# Checks if sentinel.db has exceeded the chunk threshold.
# If so, stops Sentinel, seals the current DB as the next numbered chunk,
# creates a fresh empty DB with the schema, and restarts Sentinel.
# Writes a sidecar .meta.json for each sealed chunk.
#
# Designed to be invoked by systemd timer every 15 minutes.

set -euo pipefail

# ---- Config ----
DATA_DIR="/mnt/ssd/sentinel-data"
DB="${DATA_DIR}/sentinel.db"
CHUNKS_DIR="${DATA_DIR}/chunks"
SCHEMA="/home/user/sentinel/schema.sql"
THRESHOLD_MB=500
LOG="${DATA_DIR}/logs/rotator.log"

# ---- Helpers ----
log() {
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG"
}

die() {
    log "FATAL: $*"
    echo "FATAL: $*" >&2
    exit 1
}

# ---- Pre-flight ----
mkdir -p "$CHUNKS_DIR"
mkdir -p "$(dirname "$LOG")"

[ -f "$DB" ] || die "DB not found: $DB"
[ -f "$SCHEMA" ] || die "Schema not found: $SCHEMA"

# ---- Check size ----
SIZE_BYTES=$(stat -c %s "$DB")
SIZE_MB=$(( SIZE_BYTES / 1024 / 1024 ))

if [ "$SIZE_MB" -lt "$THRESHOLD_MB" ]; then
    # Not yet time to rotate. Silent exit (don't spam logs).
    exit 0
fi

log "DB at ${SIZE_MB}MB exceeds threshold ${THRESHOLD_MB}MB - rotating"

# ---- Determine next chunk number ----
LAST_NUM=$(ls "${CHUNKS_DIR}"/sentinel-*.db 2>/dev/null \
    | sed 's/.*sentinel-0*\([0-9]*\)\.db/\1/' \
    | sort -n | tail -1)
NEXT_NUM=$(( ${LAST_NUM:-0} + 1 ))
NEXT_NAME=$(printf "sentinel-%03d" "$NEXT_NUM")
NEXT_DB="${CHUNKS_DIR}/${NEXT_NAME}.db"
NEXT_META="${CHUNKS_DIR}/${NEXT_NAME}.meta.json"

log "Sealing as ${NEXT_NAME}.db"

# ---- Stop Sentinel ----
log "Stopping Sentinel daemons"
sudo systemctl stop sentinel-wifi sentinel-bt 2>>"$LOG" || die "Failed to stop system daemons"
sudo -u kali XDG_RUNTIME_DIR=/run/user/$(id -u kali) systemctl --user --machine=user@.host stop \
    sentinel-ingest sentinel-detector sentinel-profiler.timer 2>>"$LOG" || \
    log "WARN: user units stop reported issues (may be cosmetic)"

# Brief pause to let WAL flush
sleep 2

# ---- Force WAL checkpoint so the .db is self-contained ----
log "Checkpointing WAL"
sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" >>"$LOG" 2>&1 || die "WAL checkpoint failed"

# ---- Compute metadata BEFORE moving file ----
log "Computing chunk metadata"
STARTED_AT=$(sqlite3 "$DB" "SELECT MIN(timestamp) FROM observations" 2>/dev/null || echo "unknown")
SEALED_AT=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
ROW_OBS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM observations" 2>/dev/null || echo 0)
ROW_WIFI=$(sqlite3 "$DB" "SELECT COUNT(*) FROM wifi_frames" 2>/dev/null || echo 0)
ROW_PROBE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM probe_requests" 2>/dev/null || echo 0)
ROW_BT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM bt_advertisements" 2>/dev/null || echo 0)
ROW_ADSB=$(sqlite3 "$DB" "SELECT COUNT(*) FROM sdr_adsb" 2>/dev/null || echo 0)
ROW_ALERT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM alerts" 2>/dev/null || echo 0)
ROW_DEV=$(sqlite3 "$DB" "SELECT COUNT(*) FROM devices" 2>/dev/null || echo 0)
UNIQUE_SSIDS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT ssid) FROM probe_requests WHERE ssid != ''" 2>/dev/null || echo 0)

# ---- Move the file ----
log "Moving DB to chunk location"
mv "$DB" "$NEXT_DB" || die "Failed to move DB"
# Clean up any leftover sidecars
rm -f "${DB}-wal" "${DB}-shm"

# ---- Compute checksum of sealed chunk ----
CHECKSUM=$(sha256sum "$NEXT_DB" | awk '{print $1}')
SEALED_SIZE=$(stat -c %s "$NEXT_DB")

# ---- Write meta JSON ----
cat > "$NEXT_META" <<EOF
{
  "chunk_id": ${NEXT_NUM},
  "filename": "${NEXT_NAME}.db",
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
    "sdr_adsb": ${ROW_ADSB},
    "alerts": ${ROW_ALERT}
  },
  "unique_ssids_probed": ${UNIQUE_SSIDS},
  "sha256": "${CHECKSUM}"
}
EOF

log "Sealed ${NEXT_NAME}.db (${ROW_OBS} obs, ${ROW_DEV} devices)"

# ---- Create fresh empty DB ----
log "Creating fresh sentinel.db with schema"
sqlite3 "$DB" "PRAGMA journal_mode=WAL;" >>"$LOG" 2>&1
sqlite3 "$DB" < "$SCHEMA" >>"$LOG" 2>&1 || die "Schema apply failed"

# Carry forward devices so live identification keeps working.
# Do this by attaching the previous chunk and copying.
sqlite3 "$DB" <<SQL >>"$LOG" 2>&1
ATTACH DATABASE '${NEXT_DB}' AS prev;
INSERT INTO devices SELECT * FROM prev.devices;
DETACH DATABASE prev;
SQL

DEV_CARRIED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM devices")

# ---- Fix ownership ----
chown kali:kali "$DB"

# ---- Restart Sentinel ----
log "Restarting Sentinel daemons"
sudo systemctl start sentinel-wifi sentinel-bt 2>>"$LOG" || die "Failed to restart system daemons"
sudo -u kali XDG_RUNTIME_DIR=/run/user/$(id -u kali) systemctl --user --machine=user@.host start \
    sentinel-ingest sentinel-detector sentinel-profiler.timer 2>>"$LOG" || \
    log "WARN: user units start reported issues"

log "Rotation complete: chunk ${NEXT_NUM} sealed, fresh DB live"
exit 0
