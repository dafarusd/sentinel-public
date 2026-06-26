"""Aggregator: bt_advertisements.device_name -> device_ble_names.

Mirrors probe_history.py. A single MAC can broadcast multiple names
over time (firmware updates, user renames), hence the composite PK
on (mac, device_name). NULL or empty names are skipped.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sentinel.identification._state import get_watermark, set_watermark

_NAME = "ble_names"


def aggregate_ble_names(db_path: Path) -> int:
    """Process new bt_advertisements rows into device_ble_names.

    Returns the number of (mac, device_name) pairs upserted. Returns
    0 and leaves the watermark untouched when no new rows are present.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.cursor()
            watermark = get_watermark(cur, _NAME)

            rows = cur.execute(
                """
                SELECT
                    mac,
                    device_name,
                    MIN(timestamp) AS first_ts,
                    MAX(timestamp) AS last_ts,
                    COUNT(*) AS cnt
                FROM bt_advertisements
                WHERE timestamp > ?
                  AND device_name IS NOT NULL
                  AND device_name != ''
                GROUP BY mac, device_name
                """,
                (watermark,),
            ).fetchall()

            if not rows:
                conn.rollback()
                return 0

            new_watermark = max(r["last_ts"] for r in rows)

            cur.executemany(
                """
                INSERT INTO device_ble_names
                    (mac, device_name, first_seen, last_seen, observation_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mac, device_name) DO UPDATE SET
                    last_seen = MAX(last_seen, excluded.last_seen),
                    observation_count = observation_count + excluded.observation_count
                """,
                [
                    (r["mac"], r["device_name"], r["first_ts"], r["last_ts"], r["cnt"])
                    for r in rows
                ],
            )

            set_watermark(cur, _NAME, new_watermark)
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
