"""Database schema management for Sentinel.

Applies schema.sql idempotently to the SQLite database.
Can be run directly: python -m sentinel.db.schema
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from sentinel.config import get_config, load_config


def get_schema_sql() -> str:
    """Read the schema.sql file from the project root."""
    # schema.sql lives at the repo root, next to config.yaml
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "schema.sql",
        get_config().install_dir / "schema.sql",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text()
    raise FileNotFoundError(
        f"schema.sql not found in any of: {[str(c) for c in candidates]}"
    )


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, type_spec: str
) -> bool:
    """Idempotent ALTER TABLE ADD COLUMN.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op when the table already exists,
    so new columns added to schema.sql don't propagate to live databases
    without an explicit ALTER. This helper does that ALTER only if the column
    is actually missing.

    Returns True if a column was added, False if it already existed.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_spec}")
    return True


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive migrations that CREATE TABLE IF NOT EXISTS can't cover.

    Each migration must be idempotent and non-destructive. Additive only —
    no DROP, no column rewrites, no type changes. Safe to re-run on any DB.
    """
    # Stage 14a — probe IE fingerprint hash. Column and its index are
    # both created here (not in schema.sql) because schema.sql's
    # executescript runs before migrations; an index on a not-yet-added
    # column would fail on upgraded DBs.
    _add_column_if_missing(
        conn, "probe_requests", "ie_fingerprint_hash", "TEXT"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_probe_ie_fp "
        "ON probe_requests (ie_fingerprint_hash)"
    )

    # Stage 14b — evidence source tag on probe_clusters. Existing rows
    # (all produced by the SSID-Jaccard path) are backfilled to
    # 'ssid_jaccard' via the column DEFAULT. New IE-fingerprint clusters
    # will write 'ie_fingerprint' explicitly.
    _add_column_if_missing(
        conn,
        "probe_clusters",
        "evidence_type",
        "TEXT NOT NULL DEFAULT 'ssid_jaccard'",
    )

    # Stage 14d — BLE manufacturer-data fingerprint hash. On fresh installs
    # schema.sql places this column before extra_json; SQLite's ALTER TABLE
    # can only append, so live DBs will get it at the true tail. Column
    # position in SELECT * differs between fresh vs upgraded DBs — harmless
    # given all code uses named columns. The index must also live here
    # (not in schema.sql) because executescript runs before migrations.
    _add_column_if_missing(
        conn, "bt_advertisements", "mfr_fingerprint_hash", "TEXT"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bt_mfr_fp "
        "ON bt_advertisements (mfr_fingerprint_hash)"
    )

    # Stage 15 — identity_id columns. Tagged by ingest from YAML dossier.
    # NULL means unknown (no YAML match). Indexes created here for the
    # same reason as Stage 14d: ALTER TABLE only appends, so indexes on
    # not-yet-added columns must run after the column is guaranteed.
    _add_column_if_missing(conn, "observations", "identity_id", "TEXT")
    _add_column_if_missing(conn, "wifi_frames", "identity_id", "TEXT")
    _add_column_if_missing(conn, "probe_requests", "identity_id", "TEXT")
    _add_column_if_missing(conn, "bt_advertisements", "identity_id", "TEXT")
    _add_column_if_missing(conn, "devices", "identity_id", "TEXT")
    _add_column_if_missing(conn, "sdr_tpms", "identity_id", "TEXT")
    _add_column_if_missing(conn, "sdr_weather", "identity_id", "TEXT")
    _add_column_if_missing(conn, "sdr_ism", "identity_id", "TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_identity ON observations (identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wifi_frames_identity ON wifi_frames (identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_identity ON probe_requests (identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_identity ON bt_advertisements (identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_identity ON devices (identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sdr_tpms_identity ON sdr_tpms (identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sdr_weather_identity ON sdr_weather (identity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sdr_ism_identity ON sdr_ism (identity_id)")


def apply_schema(db_path: Path | None = None) -> Path:
    """Create the database and apply the schema idempotently.

    Args:
        db_path: Override path. If None, uses config.

    Returns:
        The resolved database path.
    """
    cfg = get_config()
    if db_path is None:
        db_path = cfg.resolved_db_path

    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = get_schema_sql()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema_sql)
        _apply_additive_migrations(conn)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()
    finally:
        conn.close()

    return db_path


def verify_schema(db_path: Path | None = None) -> dict[str, bool]:
    """Verify that all expected tables exist.

    Returns:
        Dict mapping table name to existence boolean.
    """
    cfg = get_config()
    if db_path is None:
        db_path = cfg.resolved_db_path

    expected_tables = [
        "devices", "observations", "wifi_frames", "probe_requests",
        "bt_advertisements", "device_profiles", "probe_clusters",
        "probe_cluster_members", "alerts", "sessions", "gps_fixes",
        "sdr_observations", "sdr_adsb",
        "sdr_tpms", "sdr_weather", "sdr_ism",
        "device_probe_history", "device_ble_names",
        "device_identity_features", "identification_watermarks",
        "sentinel_meta",
    ]

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        existing = {row[0] for row in cursor.fetchall()}
    finally:
        conn.close()

    return {table: table in existing for table in expected_tables}


def main() -> None:
    """CLI entry point for schema management."""
    import argparse
    parser = argparse.ArgumentParser(description="Sentinel DB schema management")
    parser.add_argument("--config", "-c", help="Path to config.yaml")
    parser.add_argument("--verify", action="store_true", help="Verify schema only")
    args = parser.parse_args()

    if args.config:
        load_config(args.config)
    else:
        load_config()

    cfg = get_config()
    db_path = cfg.resolved_db_path

    if args.verify:
        results = verify_schema(db_path)
        all_ok = all(results.values())
        for table, exists in sorted(results.items()):
            status = "OK" if exists else "MISSING"
            print(f"  {table}: {status}")
        sys.exit(0 if all_ok else 1)
    else:
        path = apply_schema(db_path)
        print(f"Schema applied to {path}")
        results = verify_schema(path)
        missing = [t for t, ok in results.items() if not ok]
        if missing:
            print(f"WARNING: Missing tables: {missing}", file=sys.stderr)
            sys.exit(1)
        print(f"All {len(results)} tables verified.")


if __name__ == "__main__":
    main()
