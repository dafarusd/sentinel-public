"""IPC bus for Sentinel — Unix domain socket server.

The ingest daemon runs this server. Capture daemons connect as clients
and send newline-delimited JSON events. Each line is one event dict.

Protocol:
    - Server listens on a Unix domain socket (path from config)
    - Clients connect, send JSON lines, server acks nothing (fire-and-forget)
    - Server calls registered handlers for each parsed event
    - Clean shutdown: server closes socket, clients get BrokenPipe and reconnect or exit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from sentinel.config import get_config

logger = logging.getLogger("sentinel.bus")

# Type alias for event handlers
EventHandler = Callable[[dict[str, Any]], None]


class BusServer:
    """Async Unix domain socket server that receives JSON-line events.

    Usage:
        server = BusServer()
        server.add_handler(my_callback)
        await server.start()
        # ... runs until stop() is called
        await server.stop()
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        cfg = get_config()
        self._socket_path = socket_path or cfg.resolved_socket_path
        self._handlers: list[EventHandler] = []
        self._server: asyncio.AbstractServer | None = None
        self._event_count: int = 0
        self._error_count: int = 0

    def add_handler(self, handler: EventHandler) -> None:
        """Register a callback invoked for each received event."""
        self._handlers.append(handler)

    async def start(self) -> None:
        """Start listening on the Unix domain socket."""
        # Remove stale socket file
        if self._socket_path.exists():
            self._socket_path.unlink()

        # Ensure parent directory exists
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )

        # Make socket writable by owner only
        os.chmod(str(self._socket_path), 0o600)

        logger.info("Bus server listening on %s", self._socket_path)

    async def stop(self) -> None:
        """Stop the server and clean up the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self._socket_path.exists():
            self._socket_path.unlink()

        logger.info(
            "Bus server stopped. Events received: %d, errors: %d",
            self._event_count, self._error_count,
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a connected capture daemon."""
        peer = writer.get_extra_info("peername") or "unknown"
        logger.info("Client connected: %s", peer)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # client disconnected

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self._error_count += 1
                    logger.warning("Invalid JSON from %s: %s", peer, line[:200])
                    continue

                self._event_count += 1
                for handler in self._handlers:
                    try:
                        handler(event)
                    except Exception:
                        self._error_count += 1
                        logger.exception("Handler error processing event")
        except asyncio.CancelledError:
            pass
        except ConnectionResetError:
            logger.debug("Client %s disconnected (reset)", peer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            logger.info("Client disconnected: %s", peer)

    @property
    def event_count(self) -> int:
        """Total events received since start."""
        return self._event_count

    @property
    def error_count(self) -> int:
        """Total errors since start."""
        return self._error_count


class BusClient:
    """Async Unix domain socket client for sending JSON-line events.

    Used by capture daemons to send events to the ingest bus.

    Usage:
        client = BusClient()
        await client.connect()
        await client.send({"mac": "aa:bb:cc:dd:ee:ff", ...})
        await client.close()
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        cfg = get_config()
        self._socket_path = socket_path or cfg.resolved_socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send_count: int = 0

    async def connect(self, retries: int = 5, delay: float = 2.0) -> None:
        """Connect to the bus server with retry logic.

        Args:
            retries: Number of connection attempts before giving up.
            delay: Seconds between retries.
        """
        for attempt in range(1, retries + 1):
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    str(self._socket_path)
                )
                logger.info("Connected to bus at %s", self._socket_path)
                return
            except (ConnectionRefusedError, FileNotFoundError) as exc:
                if attempt == retries:
                    raise ConnectionError(
                        f"Cannot connect to bus at {self._socket_path} "
                        f"after {retries} attempts"
                    ) from exc
                logger.warning(
                    "Bus connection attempt %d/%d failed: %s. Retrying in %.1fs...",
                    attempt, retries, exc, delay,
                )
                await asyncio.sleep(delay)

    async def send(self, event: dict[str, Any]) -> None:
        """Send a single event to the bus.

        Args:
            event: Dict that will be serialized as a JSON line.

        Raises:
            ConnectionError: If not connected.
        """
        if self._writer is None:
            raise ConnectionError("Not connected to bus")

        line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        self._writer.write(line.encode())
        await self._writer.drain()
        self._send_count += 1

    async def close(self) -> None:
        """Close the connection."""
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            self._writer = None
            self._reader = None
            logger.info("Bus client closed. Events sent: %d", self._send_count)

    @property
    def connected(self) -> bool:
        """Whether the client appears to be connected."""
        return self._writer is not None and not self._writer.is_closing()

    @property
    def send_count(self) -> int:
        """Total events sent since connect."""
        return self._send_count
