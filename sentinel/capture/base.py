"""Base class for Sentinel capture daemons.

Provides common lifecycle: config loading, bus connection, signal handling,
logging setup, and the run loop. Subclasses implement _capture() to yield events.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from abc import ABC, abstractmethod
from typing import Any

from sentinel.common.logging import setup_logging
from sentinel.config import get_config, install_sighup_handler, load_config
from sentinel.ingest.bus import BusClient


class BaseCaptureD(ABC):
    """Abstract base for capture daemons.

    Subclasses must implement:
        name       — daemon name (e.g. 'wifi', 'bt')
        _setup()   — initialize hardware / libraries
        _capture() — async generator that yields event dicts
        _teardown()— clean up hardware state
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short daemon name used for logging, socket ID, and systemd unit."""
        ...

    def __init__(self, config_path: str | None = None) -> None:
        if config_path:
            load_config(config_path)
        self._cfg = get_config()
        self._logger = setup_logging(self.name)
        self._bus = BusClient()
        self._running = True
        self._event_count = 0

    async def run(self) -> None:
        """Main entry point — connect to bus, capture, send events."""
        install_sighup_handler()
        self._install_signal_handlers()

        self._logger.info("Starting %s capture daemon", self.name)

        try:
            await self._setup()
        except Exception:
            self._logger.exception("Setup failed for %s", self.name)
            sys.exit(1)

        try:
            await self._bus.connect()
        except ConnectionError:
            self._logger.error("Cannot connect to ingest bus — is sentinel-ingest running?")
            await self._teardown()
            sys.exit(1)

        self._logger.info("%s capture daemon running", self.name)

        try:
            async for event in self._capture():
                if not self._running:
                    break
                event["source"] = self.name
                await self._bus.send(event)
                self._event_count += 1
        except asyncio.CancelledError:
            self._logger.info("Capture cancelled")
        except (BrokenPipeError, ConnectionResetError):
            # Ingest socket died mid-session. Historically this was caught
            # by the generic Exception handler below, which logs but lets
            # run() return normally (exit 0) — systemd's Restart=on-failure
            # then doesn't fire and the capture process silently stays
            # "active" with no events flowing. Exit non-zero so systemd
            # restarts us and we reconnect cleanly. finally block below
            # still runs teardown on the way out.
            self._logger.exception(
                "Bus connection died mid-capture in %s — exiting for systemd restart",
                self.name,
            )
            sys.exit(1)
        except Exception:
            self._logger.exception("Capture error in %s", self.name)
        finally:
            self._logger.info(
                "Shutting down %s (sent %d events)", self.name, self._event_count
            )
            await self._bus.close()
            await self._teardown()

    @abstractmethod
    async def _setup(self) -> None:
        """Initialize hardware, interfaces, or libraries."""
        ...

    @abstractmethod
    async def _capture(self) -> Any:
        """Async generator yielding event dicts.

        Each dict must contain at minimum:
            timestamp — ISO 8601 UTC string
            mac       — device MAC address

        The base class adds 'source' automatically.
        """
        ...

    @abstractmethod
    async def _teardown(self) -> None:
        """Clean up hardware state (e.g. disable monitor mode, close adapters)."""
        ...

    def _install_signal_handlers(self) -> None:
        """Register SIGTERM/SIGINT for graceful shutdown."""
        loop = asyncio.get_event_loop()

        def _shutdown(sig: signal.Signals) -> None:
            self._logger.info("Received %s, shutting down", sig.name)
            self._running = False

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown, sig)

    @staticmethod
    def entrypoint(daemon_cls: type[BaseCaptureD]) -> None:
        """Standard CLI entrypoint for a capture daemon.

        Usage in a capture module:
            if __name__ == "__main__":
                BaseCaptureD.entrypoint(WifiCaptureD)
        """
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--config", "-c", help="Path to config.yaml")
        args = parser.parse_args()

        daemon = daemon_cls(config_path=args.config)
        asyncio.run(daemon.run())
