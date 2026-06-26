"""Aggregator: rebuild device_identity_features per MAC.

This is the per-MAC rollup that downstream Stage 17b-e queries build
on. Strategy: find MACs with new activity since the last watermark,
recompute their feature row from scratch, upsert. Per-MAC recompute
is cheap because the intermediate tables (devices, observations,
device_probe_history, device_ble_names) are already aggregated.

Identity matching uses the same dossier loader as the ingest path
(sentinel.identity.loader). Pass None for identity_map to skip the
dossier join entirely (test/dev paths).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sentinel.identification._state import get_watermark, set_watermark
from sentinel.identity.loader import lookup_identity

_NAME = "identity_features"


def rebuild_identity_features(
    db_path: Path, identity_map: dict[str, str] | None
) -> int:
    """Recompute device_identity_features for MACs with new observations.

    Returns the number of MAC rows upserted on this run. Returns 0 if
    no MACs have new activity since the watermark.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.cursor()
            watermark = get_watermark(cur, _NAME)

            mac_rows = cur.execute(
                "SELECT DISTINCT mac FROM observations WHERE timestamp > ?",
                (watermark,),
            ).fetchall()

            if not mac_rows:
                conn.rollback()
                return 0

            # Pin the watermark to the max timestamp visible at this
            # moment; any rows written after this point will be caught
            # on the next run.
            new_watermark_row = cur.execute(
                "SELECT MAX(timestamp) AS m FROM observations"
            ).fetchone()
            new_watermark = new_watermark_row["m"]

            count = 0
            for row in mac_rows:
                if _rebuild_one_mac(cur, row["mac"], identity_map):
                    count += 1

            set_watermark(cur, _NAME, new_watermark)
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _rebuild_one_mac(
    cur: sqlite3.Cursor,
    mac: str,
    identity_map: dict[str, str] | None,
) -> bool:
    """Compute and upsert the feature row for one MAC.

    Returns True if a row was written, False if the MAC has no
    observations (e.g. it vanished between the watermark query and
    the rebuild — race window is tiny but possible).
    """
    stats = cur.execute(
        """
        SELECT
            MIN(timestamp) AS first_seen,
            MAX(timestamp) AS last_seen,
            COUNT(*)       AS total,
            MIN(rssi)      AS rssi_min,
            MAX(rssi)      AS rssi_max,
            AVG(rssi)      AS rssi_avg,
            COUNT(DISTINCT strftime('%Y-%m-%d-%H', timestamp)) AS hours_active
        FROM observations
        WHERE mac = ?
        """,
        (mac,),
    ).fetchone()

    if stats is None or stats["total"] == 0:
        return False

    sources = cur.execute(
        "SELECT DISTINCT source FROM observations WHERE mac = ? ORDER BY source",
        (mac,),
    ).fetchall()
    sources_json = json.dumps([s["source"] for s in sources])

    vendor_row = cur.execute(
        "SELECT vendor FROM devices WHERE mac = ?", (mac,)
    ).fetchone()
    vendor = vendor_row["vendor"] if vendor_row else None

    probe_count = cur.execute(
        "SELECT COUNT(*) AS n FROM device_probe_history WHERE mac = ?",
        (mac,),
    ).fetchone()["n"]

    names = cur.execute(
        "SELECT device_name FROM device_ble_names WHERE mac = ? "
        "ORDER BY observation_count DESC",
        (mac,),
    ).fetchall()
    ble_names_json = (
        json.dumps([n["device_name"] for n in names]) if names else None
    )

    # Paired MAC candidates: same first 5 octets, differs only in last —
    # classic +1/-1 router pattern. The prefix slice is 14 chars
    # ("XX:XX:XX:XX:XX") on a canonical 17-char MAC.
    prefix = mac[:14]
    paired = cur.execute(
        "SELECT DISTINCT mac FROM observations "
        "WHERE mac LIKE ? AND mac != ?",
        (prefix + "%", mac),
    ).fetchall()
    paired_json = json.dumps([p["mac"] for p in paired]) if paired else None

    identity_id = (
        lookup_identity(mac, identity_map) if identity_map else None
    )

    cur.execute(
        """
        INSERT INTO device_identity_features (
            mac, first_seen, last_seen, total_observations, vendor,
            sources_seen, probe_ssid_count, ble_names,
            rssi_min, rssi_max, rssi_avg, hours_active,
            paired_mac_candidates, identity_id, last_updated
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')
        )
        ON CONFLICT(mac) DO UPDATE SET
            first_seen           = excluded.first_seen,
            last_seen            = excluded.last_seen,
            total_observations   = excluded.total_observations,
            vendor               = excluded.vendor,
            sources_seen         = excluded.sources_seen,
            probe_ssid_count     = excluded.probe_ssid_count,
            ble_names            = excluded.ble_names,
            rssi_min             = excluded.rssi_min,
            rssi_max             = excluded.rssi_max,
            rssi_avg             = excluded.rssi_avg,
            hours_active         = excluded.hours_active,
            paired_mac_candidates= excluded.paired_mac_candidates,
            identity_id          = excluded.identity_id,
            last_updated         = datetime('now')
        """,
        (
            mac,
            stats["first_seen"],
            stats["last_seen"],
            stats["total"],
            vendor,
            sources_json,
            probe_count,
            ble_names_json,
            stats["rssi_min"],
            stats["rssi_max"],
            stats["rssi_avg"],
            stats["hours_active"],
            paired_json,
            identity_id,
        ),
    )
    return True
