"""Sentinel query API — structured data access.

All functions accept a read-only sqlite3.Connection and return
plain dicts/lists. No formatting, no side effects, no printing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def get_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Get overall system status summary."""
    now = datetime.now(timezone.utc)

    device_count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    observation_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    alert_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    unacked_alerts = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE acknowledged = 0"
    ).fetchone()[0]

    # Recent activity (last hour)
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    recent_obs = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE timestamp >= ?", (one_hour_ago,)
    ).fetchone()[0]
    recent_alerts = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE timestamp >= ?", (one_hour_ago,)
    ).fetchone()[0]

    # Active devices (seen in last hour)
    active_devices = conn.execute(
        "SELECT COUNT(DISTINCT mac) FROM observations WHERE timestamp >= ?",
        (one_hour_ago,),
    ).fetchone()[0]

    # Database info
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    # Learning mode check
    meta_row = conn.execute(
        "SELECT value FROM sentinel_meta WHERE key = 'installed_at'"
    ).fetchone()
    installed_at = meta_row["value"] if meta_row else None
    schema_version = conn.execute(
        "SELECT value FROM sentinel_meta WHERE key = 'schema_version'"
    ).fetchone()

    # Table sizes
    profile_count = conn.execute("SELECT COUNT(*) FROM device_profiles").fetchone()[0]
    cluster_count = conn.execute("SELECT COUNT(*) FROM probe_clusters").fetchone()[0]

    return {
        "timestamp": now.isoformat(),
        "db_path": db_path,
        "schema_version": schema_version["value"] if schema_version else None,
        "installed_at": installed_at,
        "device_count": device_count,
        "observation_count": observation_count,
        "alert_count": alert_count,
        "unacked_alerts": unacked_alerts,
        "profile_count": profile_count,
        "cluster_count": cluster_count,
        "last_hour": {
            "observations": recent_obs,
            "alerts": recent_alerts,
            "active_devices": active_devices,
        },
    }


# ---------------------------------------------------------------------------
# SDR / ADS-B (Stage 18b)
# ---------------------------------------------------------------------------

def get_adsb_summary(
    conn: sqlite3.Connection, enabled: bool
) -> dict[str, Any]:
    """Summarize the ADS-B subsystem for the status CLI.

    Three display states are encoded in the returned dict:
        - disabled:   enabled=False, has_data=False
        - enabled, no data: enabled=True, has_data=False
        - enabled with data: has_data=True + counts + latest observation

    Gracefully returns the "no data yet" shape if the sdr_adsb table
    is missing (e.g., an old DB that hasn't had the schema re-applied).
    """
    summary: dict[str, Any] = {
        "enabled": enabled,
        "has_data": False,
        "last_hour_messages": 0,
        "last_hour_aircraft": 0,
        "total_messages": 0,
        "latest_observation": None,
        "latest_icao": None,
    }

    if not enabled:
        return summary

    # Table existence check — apply_schema runs on every ingest start
    # so this only fails on a stale DB from before Stage 18b.
    table_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sdr_adsb'"
    ).fetchone()
    if table_row is None:
        return summary

    total = conn.execute("SELECT COUNT(*) FROM sdr_adsb").fetchone()[0]
    if total == 0:
        return summary

    now = datetime.now(timezone.utc)
    one_hour_ago = (now - timedelta(hours=1)).isoformat()

    last_hour_msgs, last_hour_aircraft = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT icao_hex) "
        "FROM sdr_adsb WHERE timestamp >= ?",
        (one_hour_ago,),
    ).fetchone()

    latest = conn.execute(
        "SELECT timestamp, icao_hex FROM sdr_adsb "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    summary.update({
        "has_data": True,
        "last_hour_messages": last_hour_msgs or 0,
        "last_hour_aircraft": last_hour_aircraft or 0,
        "total_messages": total,
        "latest_observation": latest["timestamp"] if latest else None,
        "latest_icao": latest["icao_hex"] if latest else None,
    })
    return summary


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

def list_devices(
    conn: sqlite3.Connection,
    since: str | None = None,
    seen_in: float | None = None,
    vendor: str | None = None,
    new_since: str | None = None,
    device_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List devices with optional filters.

    Args:
        since: Only devices last seen after this ISO timestamp.
        seen_in: Only devices seen in the last N hours.
        vendor: Filter by vendor name (substring match).
        new_since: Only devices first seen after this ISO timestamp.
        device_type: Filter by device_type (wifi, ble, bt_classic).
        limit: Maximum results.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if since:
        clauses.append("d.last_seen >= ?")
        params.append(since)
    elif seen_in is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=seen_in)).isoformat()
        clauses.append("d.last_seen >= ?")
        params.append(cutoff)

    if vendor:
        clauses.append("d.vendor LIKE ?")
        params.append(f"%{vendor}%")

    if new_since:
        clauses.append("d.first_seen >= ?")
        params.append(new_since)

    if device_type:
        clauses.append("d.device_type = ?")
        params.append(device_type)

    where = " AND ".join(clauses) if clauses else "1=1"

    rows = conn.execute(
        f"SELECT d.*, "
        f"(SELECT COUNT(*) FROM observations WHERE mac = d.mac) as obs_count "
        f"FROM devices d WHERE {where} "
        f"ORDER BY d.last_seen DESC LIMIT ?",
        (*params, limit),
    ).fetchall()

    return [dict(r) for r in rows]


def get_device(conn: sqlite3.Connection, mac: str) -> dict[str, Any] | None:
    """Get detailed info for a single device."""
    dev = conn.execute("SELECT * FROM devices WHERE mac = ?", (mac,)).fetchone()
    if dev is None:
        return None

    result = dict(dev)

    # Observation count and recent observations
    result["observation_count"] = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE mac = ?", (mac,)
    ).fetchone()[0]

    result["recent_observations"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM observations WHERE mac = ? ORDER BY timestamp DESC LIMIT 20",
            (mac,),
        ).fetchall()
    ]

    # Profile
    profile = conn.execute(
        "SELECT * FROM device_profiles WHERE mac = ?", (mac,)
    ).fetchone()
    if profile:
        p = dict(profile)
        for field in ("time_histogram", "channel_set", "probe_ssid_set", "companion_macs"):
            if p.get(field):
                p[field] = json.loads(p[field])
        result["profile"] = p
    else:
        result["profile"] = None

    # Probe cluster membership
    membership = conn.execute(
        "SELECT pcm.cluster_id, pcm.jaccard_score, pc.ssid_set, pc.device_count "
        "FROM probe_cluster_members pcm "
        "JOIN probe_clusters pc ON pcm.cluster_id = pc.cluster_id "
        "WHERE pcm.mac = ?",
        (mac,),
    ).fetchone()
    if membership:
        m = dict(membership)
        if m.get("ssid_set"):
            m["ssid_set"] = json.loads(m["ssid_set"])
        result["probe_cluster"] = m
    else:
        result["probe_cluster"] = None

    # Recent alerts
    result["recent_alerts"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM alerts WHERE mac = ? ORDER BY timestamp DESC LIMIT 10",
            (mac,),
        ).fetchall()
    ]

    return result


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def list_alerts(
    conn: sqlite3.Connection,
    severity: str | None = None,
    alert_type: str | None = None,
    since: str | None = None,
    mac: str | None = None,
    unacked_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List alerts with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if alert_type:
        clauses.append("alert_type = ?")
        params.append(alert_type)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if mac:
        clauses.append("mac = ?")
        params.append(mac)
    if unacked_only:
        clauses.append("acknowledged = 0")

    where = " AND ".join(clauses) if clauses else "1=1"

    rows = conn.execute(
        f"SELECT * FROM alerts WHERE {where} ORDER BY timestamp DESC LIMIT ?",
        (*params, limit),
    ).fetchall()

    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Watch (live tail)
# ---------------------------------------------------------------------------

def get_new_alerts(
    conn: sqlite3.Connection,
    after_id: int = 0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get alerts with id > after_id (for live tailing)."""
    rows = conn.execute(
        "SELECT * FROM alerts WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Raw query
# ---------------------------------------------------------------------------

def execute_readonly_query(
    conn: sqlite3.Connection,
    sql: str,
    limit: int = 100,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute a read-only SQL query.

    Returns (column_names, rows).
    Automatically appends LIMIT if not present.
    """
    sql_stripped = sql.strip().rstrip(";")
    if "limit" not in sql_stripped.lower():
        sql_stripped += f" LIMIT {limit}"

    cursor = conn.execute(sql_stripped)
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    rows = cursor.fetchall()
    # Convert Row objects to plain tuples
    return columns, [tuple(r) for r in rows]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_table(
    conn: sqlite3.Connection,
    table: str,
    since: str | None = None,
    limit: int = 10000,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Export a table's data as a list of dicts.

    Returns (column_names, rows_as_dicts).
    """
    valid_tables = {
        "devices", "observations", "wifi_frames", "probe_requests",
        "bt_advertisements", "device_profiles", "probe_clusters",
        "probe_cluster_members", "alerts", "sessions", "gps_fixes",
        "sdr_observations", "sdr_adsb",
    }
    if table not in valid_tables:
        raise ValueError(f"Invalid table: {table}. Valid: {sorted(valid_tables)}")

    if since and table not in ("devices", "device_profiles", "probe_clusters"):
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
            (since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()

    if not rows:
        return [], []

    columns = rows[0].keys()
    return list(columns), [dict(r) for r in rows]
