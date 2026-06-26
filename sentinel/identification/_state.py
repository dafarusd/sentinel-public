"""Watermark helpers for identification aggregators.

Each aggregator reads its last-processed timestamp before a run and
writes the new high-water mark back at the end of the same transaction.
"""

from __future__ import annotations

import sqlite3

# Used when no prior watermark exists for an aggregator. ISO-8601
# string so lexicographic comparison against `timestamp` columns is
# chronologically correct.
EPOCH_TS = "1970-01-01T00:00:00+00:00"


def get_watermark(cur: sqlite3.Cursor, name: str) -> str:
    """Return the last_processed_ts for `name`, or EPOCH_TS if unset."""
    row = cur.execute(
        "SELECT last_processed_ts FROM identification_watermarks "
        "WHERE aggregator = ?",
        (name,),
    ).fetchone()
    if row is None:
        return EPOCH_TS
    return row[0] if not isinstance(row, sqlite3.Row) else row["last_processed_ts"]


def set_watermark(cur: sqlite3.Cursor, name: str, ts: str) -> None:
    """Upsert watermark for `name`. Updates last_run_ts to now."""
    cur.execute(
        "INSERT INTO identification_watermarks "
        "(aggregator, last_processed_ts, last_run_ts) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(aggregator) DO UPDATE SET "
        "    last_processed_ts = excluded.last_processed_ts, "
        "    last_run_ts = excluded.last_run_ts",
        (name, ts),
    )
