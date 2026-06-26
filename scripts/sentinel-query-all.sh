#!/bin/bash
# sentinel-query-all.sh
# Runs a SQL query across the current sentinel.db AND all sealed chunks.
# Usage:
#   sentinel-query-all.sh "SELECT mac FROM devices WHERE vendor LIKE 'Apple%'"
#   sentinel-query-all.sh -f query.sql
#   sentinel-query-all.sh --find-mac aa:bb:cc:00:00:01
#
# For cross-chunk UNION queries, reference each attached chunk explicitly:
#   main.observations   - current DB
#   c001.observations   - sentinel-001.db
#   c002.observations   - sentinel-002.db
#   etc.
#
# Shortcut mode: --find-mac builds a canonical "has this MAC ever appeared" query.

set -euo pipefail

DATA_DIR="/mnt/ssd/sentinel-data"
DB="${DATA_DIR}/sentinel.db"
CHUNKS_DIR="${DATA_DIR}/chunks"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] "SQL QUERY"

Options:
  -f FILE            Read SQL from file instead of argument
  --find-mac MAC     Canonical "has this MAC ever appeared" search
  --list-chunks      Just list all chunks with metadata, don't query
  --attach-names     Print the ATTACH aliases (c001, c002, ...) without running anything
  -h, --help         This message

Examples:
  $(basename "$0") "SELECT COUNT(*) FROM main.observations"
  $(basename "$0") --find-mac aa:bb:cc:00:00:01
  $(basename "$0") -f ~/my-query.sql
EOF
    exit 1
}

# ---- Parse args ----
MODE="inline"
QUERY=""
TARGET_MAC=""

while [ $# -gt 0 ]; do
    case "$1" in
        -f)
            MODE="file"
            QUERY_FILE="$2"
            shift 2
            ;;
        --find-mac)
            MODE="find-mac"
            TARGET_MAC="$2"
            shift 2
            ;;
        --list-chunks)
            MODE="list"
            shift
            ;;
        --attach-names)
            MODE="names"
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            QUERY="$1"
            shift
            ;;
    esac
done

# ---- Gather chunks ----
CHUNKS=()
if [ -d "$CHUNKS_DIR" ]; then
    while IFS= read -r f; do
        CHUNKS+=("$f")
    done < <(ls "${CHUNKS_DIR}"/sentinel-*.db 2>/dev/null | sort)
fi

# ---- List mode ----
if [ "$MODE" = "list" ]; then
    echo "=== Current DB ==="
    if [ -f "$DB" ]; then
        SIZE=$(du -h "$DB" | awk '{print $1}')
        ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM observations" 2>/dev/null || echo "?")
        echo "  ${DB}  ${SIZE}  ${ROWS} observations"
    else
        echo "  (none - DB missing)"
    fi
    echo ""
    echo "=== Sealed chunks ==="
    if [ ${#CHUNKS[@]} -eq 0 ]; then
        echo "  (none yet)"
    else
        for c in "${CHUNKS[@]}"; do
            META="${c%.db}.meta.json"
            if [ -f "$META" ]; then
                SIZE=$(du -h "$c" | awk '{print $1}')
                SEALED=$(grep -o '"sealed_at": "[^"]*"' "$META" | cut -d'"' -f4)
                OBS=$(grep -o '"observations": [0-9]*' "$META" | grep -o '[0-9]*')
                echo "  $(basename "$c")  ${SIZE}  sealed=${SEALED}  obs=${OBS}"
            else
                SIZE=$(du -h "$c" | awk '{print $1}')
                echo "  $(basename "$c")  ${SIZE}  (no metadata)"
            fi
        done
    fi
    exit 0
fi

# ---- Attach-names mode ----
if [ "$MODE" = "names" ]; then
    echo "main               - ${DB}"
    i=0
    for c in "${CHUNKS[@]}"; do
        i=$((i+1))
        NAME=$(basename "$c" .db | sed 's/sentinel-/c/')
        echo "${NAME}               - ${c}"
    done
    exit 0
fi

# ---- Build ATTACH statements ----
ATTACH_SQL=""
for c in "${CHUNKS[@]}"; do
    NAME=$(basename "$c" .db | sed 's/sentinel-/c/')
    ATTACH_SQL="${ATTACH_SQL}ATTACH DATABASE '${c}' AS ${NAME};"$'\n'
done

# ---- Build the final query ----
case "$MODE" in
    inline)
        if [ -z "$QUERY" ]; then
            echo "ERROR: No query provided" >&2
            usage
        fi
        FINAL_SQL="${ATTACH_SQL}${QUERY}"
        ;;
    file)
        [ -f "$QUERY_FILE" ] || { echo "ERROR: Query file not found: $QUERY_FILE" >&2; exit 1; }
        FINAL_SQL="${ATTACH_SQL}$(cat "$QUERY_FILE")"
        ;;
    find-mac)
        # Build a UNION query across main and all chunks
        UNION_PARTS="SELECT 'main' AS source, MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen, COUNT(*) AS obs FROM main.observations WHERE mac='${TARGET_MAC}'"
        for c in "${CHUNKS[@]}"; do
            NAME=$(basename "$c" .db | sed 's/sentinel-/c/')
            UNION_PARTS="${UNION_PARTS} UNION ALL SELECT '${NAME}' AS source, MIN(timestamp), MAX(timestamp), COUNT(*) FROM ${NAME}.observations WHERE mac='${TARGET_MAC}'"
        done
        FINAL_SQL="${ATTACH_SQL}SELECT * FROM (${UNION_PARTS}) WHERE obs > 0 ORDER BY first_seen;"
        ;;
esac

# ---- Execute ----
sqlite3 -column -header "$DB" <<EOF
${FINAL_SQL}
EOF
