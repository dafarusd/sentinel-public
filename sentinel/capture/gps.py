"""GPS/LoRa capture daemon stub for Sentinel.

Placeholder for future Waveshare SX1262 + NEO-6M GPS hat capture.
Checks config flag, logs status, and exits gracefully.

Usage:
    python -m sentinel.capture.gps
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, AsyncIterator

from sentinel.capture.base import BaseCaptureD
from sentinel.config import get_config

logger = logging.getLogger("sentinel.gps")


class GpsCaptureD(BaseCaptureD):
    """GPS/LoRa capture daemon — stub implementation."""

    @property
    def name(self) -> str:
        return "gps"

    async def _setup(self) -> None:
        """Check if GPS is enabled in config."""
        cfg = get_config()
        if not cfg.gps.enabled:
            self._logger.info("GPS capture disabled in config (gps.enabled=false)")
            raise SystemExit(0)

        self._logger.warning(
            "GPS capture enabled in config but not yet implemented. "
            "Set gps.enabled=false to suppress this warning."
        )
        raise SystemExit(0)

    async def _capture(self) -> AsyncIterator[dict[str, Any]]:
        """Not implemented — yields nothing."""
        return
        yield  # Make this a generator

    async def _teardown(self) -> None:
        """Nothing to clean up."""
        pass


if __name__ == "__main__":
    BaseCaptureD.entrypoint(GpsCaptureD)
