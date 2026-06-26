"""Stage 17a runner.

Called from sentinel.profiler.engine.run_profiler at the end of each
tick. Runs probe_history and ble_names aggregators on every call;
identity_features rollup is rate-limited to at most once per
_FEATURES_INTERVAL_S because per-MAC recompute is the heaviest pass.

Each aggregator opens its own short-lived connection with
BEGIN IMMEDIATE — exceptions in one do not invalidate the others.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from sentinel.identification.aggregator import rebuild_identity_features
from sentinel.identification.ble_names import aggregate_ble_names
from sentinel.identification.probe_history import aggregate_probe_history

logger = logging.getLogger(__name__)

# Module-level state: when did we last rebuild identity_features?
# Persists across runner invocations within one profiler process and
# resets on profiler restart. The 5-minute rate limit matches the
# stage spec; the profiler timer fires every 15 min by default, so in
# practice this means "every tick" anyway — the guard exists so
# manual back-to-back invocations don't double-work.
_last_features_rebuild: float = 0.0
_FEATURES_INTERVAL_S = 300.0


def run_incremental(
    db_path: Path, identity_map: dict[str, str] | None = None
) -> dict[str, Any]:
    """Run all enabled identification aggregators.

    Returns a dict of per-aggregator counts (or sentinels) suitable
    for logging. Idempotent: safe to call repeatedly. Per-aggregator
    failures are logged but do not abort the others.
    """
    global _last_features_rebuild
    result: dict[str, Any] = {}

    try:
        result["probe_history"] = aggregate_probe_history(db_path)
    except Exception:
        logger.exception("probe_history aggregation failed")
        result["probe_history"] = -1

    try:
        result["ble_names"] = aggregate_ble_names(db_path)
    except Exception:
        logger.exception("ble_names aggregation failed")
        result["ble_names"] = -1

    now = time.monotonic()
    if now - _last_features_rebuild >= _FEATURES_INTERVAL_S:
        try:
            result["identity_features"] = rebuild_identity_features(
                db_path, identity_map
            )
            _last_features_rebuild = now
        except Exception:
            logger.exception("identity_features rebuild failed")
            result["identity_features"] = -1
    else:
        result["identity_features"] = "skipped (interval)"

    return result
