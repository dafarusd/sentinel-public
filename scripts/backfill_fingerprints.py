#!/usr/bin/env python3
"""One-shot backfill: compute ie_fingerprint_hash for historical probe_requests.

Walks all rows where ``ie_bytes IS NOT NULL AND ie_fingerprint_hash IS NULL``
in id-ordered batches, hashes each row's IE bytes using the same canonical
fingerprint function the live capture path uses, and writes the result back.

Idempotent: safe to re-run. A fresh invocation starts from id=0 and the
existing-hash filter skips already-processed rows. Unfingerprintable rows
(IE bytes present but no allow-listed structure) stay NULL by design —
the id cursor prevents re-processing them on the same run.

Usage:
    python scripts/backfill_fingerprints.py
    python scripts/backfill_fingerprints.py --config /path/to/config.yaml
    python scripts/backfill_fingerprints.py --dry-run
    python scripts/backfill_fingerprints.py --limit 10000
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

# Make the script runnable both as `python scripts/backfill_fingerprints.py`
# and via `python -m scripts.backfill_fingerprints`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sentinel.config import get_config, load_config
from sentinel.profiler.fingerprint import ie_fingerprint_hash

logger = logging.getLogger("sentinel.backfill_fingerprints")

_BATCH_SIZE = 5000
_PROGRESS_INTERVAL = 10_000


def _select_batch(
    conn: sqlite3.Connection, after_id: int, batch_size: int
) -> list[tuple[int, bytes]]:
    """Fetch the next batch of rows needing a fingerprint hash.

    Returns (id, ie_bytes) tuples in ascending id order. Cursor pagination
    by id guarantees forward progress even when some rows yield no hash
    (canonical returns None → stored NULL → still matches the filter).
    """
    rows = conn.execute(
        "SELECT id, ie_bytes FROM probe_requests "
        "WHERE ie_bytes IS NOT NULL "
        "  AND ie_fingerprint_hash IS NULL "
        "  AND id > ? "
        "ORDER BY id "
        "LIMIT ?",
        (after_id, batch_size),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _apply_batch(
    conn: sqlite3.Connection, updates: list[tuple[str, int]]
) -> None:
    """Apply a batch of UPDATEs inside a single immediate-lock transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "UPDATE probe_requests SET ie_fingerprint_hash = ? WHERE id = ?",
            updates,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def backfill(
    db_path: Path,
    batch_size: int = _BATCH_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the backfill and return counters.

    Args:
        db_path: SQLite DB to update.
        batch_size: Rows per SELECT/UPDATE batch.
        limit: Stop after processing this many rows total (None = all).
        dry_run: Compute hashes but don't write.

    Returns:
        Dict of counters: scanned, hashed, null_result, written.
    """
    stats = {"scanned": 0, "hashed": 0, "null_result": 0, "written": 0}
    last_id = 0
    last_progress = 0
    started = time.monotonic()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")

        while True:
            if limit is not None and stats["scanned"] >= limit:
                break

            effective_batch = batch_size
            if limit is not None:
                effective_batch = min(batch_size, limit - stats["scanned"])

            batch = _select_batch(conn, last_id, effective_batch)
            if not batch:
                break

            updates: list[tuple[str, int]] = []
            for row_id, ie_blob in batch:
                stats["scanned"] += 1
                last_id = row_id
                # ie_bytes is declared BLOB — sqlite3 returns it as bytes,
                # but memoryview is also possible; normalize.
                if isinstance(ie_blob, memoryview):
                    ie_blob = bytes(ie_blob)
                fp = ie_fingerprint_hash(ie_blob)
                if fp is None:
                    stats["null_result"] += 1
                    continue
                stats["hashed"] += 1
                updates.append((fp, row_id))

            if updates and not dry_run:
                _apply_batch(conn, updates)
                stats["written"] += len(updates)

            if stats["scanned"] - last_progress >= _PROGRESS_INTERVAL:
                elapsed = time.monotonic() - started
                rate = stats["scanned"] / elapsed if elapsed > 0 else 0.0
                logger.info(
                    "scanned=%d hashed=%d null=%d written=%d  (%.0f rows/s)",
                    stats["scanned"],
                    stats["hashed"],
                    stats["null_result"],
                    stats["written"],
                    rate,
                )
                last_progress = stats["scanned"]
    finally:
        conn.close()

    elapsed = time.monotonic() - started
    logger.info(
        "Backfill complete: scanned=%d hashed=%d null=%d written=%d in %.1fs",
        stats["scanned"],
        stats["hashed"],
        stats["null_result"],
        stats["written"],
        elapsed,
    )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill ie_fingerprint_hash on historical probe_requests."
    )
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute hashes and print stats, but don't write to the DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after processing this many rows (useful for testing).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_BATCH_SIZE,
        help=f"Rows per batch (default: {_BATCH_SIZE}).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    load_config(args.config)
    cfg = get_config()
    db_path = cfg.resolved_db_path

    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    logger.info(
        "Starting backfill on %s (batch_size=%d, limit=%s, dry_run=%s)",
        db_path,
        args.batch_size,
        args.limit,
        args.dry_run,
    )

    stats = backfill(
        db_path=db_path,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    print(f"scanned:     {stats['scanned']}")
    print(f"hashed:      {stats['hashed']}")
    print(f"null_result: {stats['null_result']}")
    print(f"written:     {stats['written']}")


if __name__ == "__main__":
    main()
