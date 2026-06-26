"""Aggregator: probe_requests -> device_probe_history.

Walks every probe_request row newer than the last watermark, groups by
(mac, ssid), and upserts the (first/last/count) triple into
device_probe_history. Rows with NULL or empty ssid are skipped — those
are broadcast probes (no target SSID), which carry no identification
signal beyond the burst itself.

Idempotent: re-running with no new rows is a no-op (watermark
prevents reprocessing). On a fresh DB the watermark defaults to
the Unix epoch, so the first run sweeps the full history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sentinel.identification._state import get_watermark, set_watermark

_NAME = "probe_history"


def aggregate_probe_history(db_path: Path) -> int:
    """Process new probe_request rows into device_probe_history.

    Opens its own connection with BEGIN IMMEDIATE so upserts and the
    watermark update commit atomically. Returns the number of
    (mac, ssid) pairs upserted on this run. Returns 0 when no new
    rows are available (watermark unchanged).
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
                    ssid,
                    MIN(timestamp) AS first_ts,
                    MAX(timestamp) AS last_ts,
                    COUNT(*) AS cnt
                FROM probe_requests
                WHERE timestamp > ?
                  AND ssid IS NOT NULL
                  AND ssid != ''
                GROUP BY mac, ssid
                """,
                (watermark,),
            ).fetchall()

            if not rows:
                conn.rollback()
                return 0

            # MAX(last_probed, excluded.last_probed) below defends
            # against late-arriving out-of-order rows; SQLite MAX on
            # ISO-8601 TEXT is chronological.
            new_watermark = max(r["last_ts"] for r in rows)

            cur.executemany(
                """
                INSERT INTO device_probe_history
                    (mac, ssid, first_probed, last_probed, probe_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mac, ssid) DO UPDATE SET
                    last_probed = MAX(last_probed, excluded.last_probed),
                    probe_count = probe_count + excluded.probe_count
                """,
                [
                    (r["mac"], r["ssid"], r["first_ts"], r["last_ts"], r["cnt"])
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
