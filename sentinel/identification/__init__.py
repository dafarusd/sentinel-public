"""Cross-modality identification engine (Stage 17).

Aggregates raw capture data into per-MAC feature rows that downstream
stages (17b co-arrival, 17c rhythm, 17d candidates, 17e payload decode)
build on.

All aggregators are INCREMENTAL based on a per-aggregator watermark
stored in identification_watermarks. Each aggregator opens its own
short-lived connection with BEGIN IMMEDIATE so its upserts and
watermark update commit atomically: a crash mid-run is recoverable
without double-counting.

Called from sentinel.profiler.engine.run_profiler at the end of each
tick. Failure is non-fatal — wrapped in try/except by the caller.
"""

from sentinel.identification.runner import run_incremental

__all__ = ["run_incremental"]
