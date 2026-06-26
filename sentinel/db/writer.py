"""Single-writer thread for all Sentinel database operations.

All DB writes go through the DatabaseWriter queue to avoid SQLite locking
contention. Reads can use separate connections opened in read-only mode.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sentinel.config import get_config

logger = logging.getLogger("sentinel.db.writer")


@dataclass
class WriteOp:
    """A queued database write operation."""

    sql: str
    params: Sequence[Any] = ()
    many: bool = False  # True for executemany (params is list of tuples)


class DatabaseWriter:
    """Thread-safe single-writer for the Sentinel SQLite database.

    Usage:
        writer = DatabaseWriter(db_path)
        writer.start()
        writer.execute("INSERT INTO devices ...", (mac, ...))
        writer.execute_many("INSERT INTO observations ...", batch)
        writer.stop()  # flushes remaining ops and closes
    """

    def __init__(self, db_path: Path | None = None) -> None:
        cfg = get_config()
        self._db_path = db_path or cfg.resolved_db_path
        self._queue: queue.Queue[WriteOp | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        """Start the writer thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(
            target=self._run, name="db-writer", daemon=True
        )
        self._thread.start()
        logger.info("Database writer started: %s", self._db_path)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the writer to flush and stop."""
        self._running.clear()
        self._queue.put(None)  # sentinel to unblock
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            logger.info("Database writer stopped.")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Queue a single write operation."""
        self._queue.put(WriteOp(sql=sql, params=params))

    def execute_many(self, sql: str, params_list: list[Sequence[Any]]) -> None:
        """Queue a batch write operation (executemany)."""
        self._queue.put(WriteOp(sql=sql, params=params_list, many=True))

    def _run(self) -> None:
        """Writer thread main loop."""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")

        try:
            while self._running.is_set() or not self._queue.empty():
                try:
                    op = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                if op is None:
                    break

                try:
                    if op.many:
                        conn.executemany(op.sql, op.params)
                    else:
                        conn.execute(op.sql, op.params)
                    conn.commit()
                except sqlite3.Error:
                    logger.exception("DB write failed: %s", op.sql[:100])
        finally:
            # Flush any remaining items
            while not self._queue.empty():
                op = self._queue.get_nowait()
                if op is None:
                    continue
                try:
                    if op.many:
                        conn.executemany(op.sql, op.params)
                    else:
                        conn.execute(op.sql, op.params)
                    conn.commit()
                except sqlite3.Error:
                    logger.exception("DB flush write failed: %s", op.sql[:100])
            conn.close()

    @property
    def pending(self) -> int:
        """Number of queued write operations."""
        return self._queue.qsize()


def get_readonly_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a read-only connection to the database.

    Safe to use from any thread — does not conflict with the writer.
    """
    cfg = get_config()
    if db_path is None:
        db_path = cfg.resolved_db_path
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
