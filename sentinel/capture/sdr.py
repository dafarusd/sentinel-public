"""SDR capture daemon stub for Sentinel.

Placeholder for future RTL-SDR broadband RF survey capture.
Checks config flag, logs status, and exits gracefully.

Usage:
    python -m sentinel.capture.sdr
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, AsyncIterator

from sentinel.capture.base import BaseCaptureD
from sentinel.config import get_config

logger = logging.getLogger("sentinel.sdr")


class SdrCaptureD(BaseCaptureD):
    """SDR capture daemon — stub implementation."""

    @property
    def name(self) -> str:
        return "sdr"

    async def _setup(self) -> None:
        """Check if SDR is enabled in config."""
        cfg = get_config()
        if not cfg.sdr.enabled:
            self._logger.info("SDR capture disabled in config (sdr.enabled=false)")
            raise SystemExit(0)

        self._logger.warning(
            "SDR capture enabled in config but not yet implemented. "
            "Set sdr.enabled=false to suppress this warning."
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
    BaseCaptureD.entrypoint(SdrCaptureD)
