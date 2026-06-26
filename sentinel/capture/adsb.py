"""ADS-B capture daemon for Sentinel.

Connects to a local readsb instance's SBS-1 BaseStation TCP stream
(default 127.0.0.1:30003) and emits one event per decoded aircraft
message. Events flow through the standard ingest bus; the ingest
daemon routes them to the ``sdr_adsb`` table.

Runs as a normal user — no raw sockets, no hardware access. The
underlying SDR + RTL driver chain is owned by the distro-packaged
``readsb.service``; this daemon is purely a TCP client.

SBS-1 reference: comma-separated fields, one line per message. Only
``MSG`` records are ingested. Status/admin lines (``SEL``, ``ID``,
``AIR``, ``STA``, ``CLK``) are silently discarded. Every integer
and float conversion is wrapped in try/except because the line
source is untrusted network input — a malformed line logs a warning
and is skipped; the connection is never torn down by a parse error.

Usage:
    python -m sentinel.capture.adsb
    python -m sentinel.capture.adsb --config /path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sentinel.capture.base import BaseCaptureD

logger = logging.getLogger("sentinel.sdr_adsb")

# SBS-1 field indices (0-based). Kept as named constants so a field
# shift in the source stream is a one-line fix instead of a grep hunt.
_SBS_MSG_TYPE = 0            # "MSG", "SEL", "ID", "AIR", "STA", "CLK"
_SBS_TRANSMISSION_TYPE = 1   # 1-8 for MSG
_SBS_SESSION_ID = 2
_SBS_AIRCRAFT_ID = 3
_SBS_ICAO = 4                # 6 hex chars
_SBS_FLIGHT_ID = 5
_SBS_GENERATED_DATE = 6
_SBS_GENERATED_TIME = 7
_SBS_LOGGED_DATE = 8
_SBS_LOGGED_TIME = 9
_SBS_CALLSIGN = 10
_SBS_ALTITUDE = 11
_SBS_GROUND_SPEED = 12
_SBS_TRACK = 13
_SBS_LATITUDE = 14
_SBS_LONGITUDE = 15
_SBS_VERTICAL_RATE = 16
_SBS_SQUAWK = 17
_SBS_ALERT = 18
_SBS_EMERGENCY = 19
_SBS_SPI = 20
_SBS_IS_ON_GROUND = 21
_SBS_MIN_FIELDS = 22

# Reconnect backoff cap. The configured base delay doubles each failure
# until it hits this ceiling, then stays pinned. Picked to be short
# enough that readsb coming back online is noticed within a minute.
_RECONNECT_MAX_S = 60.0

# Parser-warning throttle: log at most one warning per this many bad
# lines. readsb streams are usually clean but a corrupt upstream could
# flood the log otherwise.
_PARSE_WARN_EVERY = 100


def _now_iso() -> str:
    """Current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _clean(value: str) -> str | None:
    """Normalize a raw SBS-1 field: strip whitespace, empty string -> None."""
    stripped = value.strip()
    return stripped if stripped else None


def _try_int(value: str | None) -> int | None:
    """Parse an integer or return None on any failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _try_float(value: str | None) -> float | None:
    """Parse a float or return None on any failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _try_bool_int(value: str | None) -> int | None:
    """Parse an SBS-1 boolean ("0"/"1") into 0/1; None on failure.

    Stored as INTEGER in extra_json for compactness; NULL where absent.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned in ("0", "1"):
        return int(cleaned)
    return None


def _combine_sbs_timestamp(date_field: str | None, time_field: str | None) -> str | None:
    """Join SBS-1 date (YYYY/MM/DD) + time (HH:MM:SS.SSS) into ISO 8601 UTC.

    readsb emits these in UTC. Returns None on any parse failure so the
    caller can fall back to ``datetime.now(timezone.utc)``.
    """
    if not date_field or not time_field:
        return None
    try:
        ts = datetime.strptime(
            f"{date_field.strip()} {time_field.strip()}",
            "%Y/%m/%d %H:%M:%S.%f",
        ).replace(tzinfo=timezone.utc)
        return ts.isoformat()
    except (ValueError, TypeError):
        return None


def _parse_sbs1(line: str) -> dict[str, Any] | None:
    """Parse one SBS-1 line into a Sentinel event dict, or None.

    Returns None for:
        - non-MSG record types (status/admin lines)
        - lines with fewer than 22 comma-separated fields
        - lines missing the ICAO field
        - any top-level parse exception

    Never raises. Per-field integer/float conversions are each wrapped
    in try/except; malformed numerics become None in the event rather
    than breaking the whole record.
    """
    if not line:
        return None

    # Defensive outer try — if anything below blows up unexpectedly we
    # log once and return None rather than kill the read loop.
    try:
        fields = line.split(",")
        if len(fields) < _SBS_MIN_FIELDS:
            return None

        if fields[_SBS_MSG_TYPE].strip() != "MSG":
            return None  # SEL/ID/AIR/STA/CLK — status lines, not aircraft data

        icao_raw = _clean(fields[_SBS_ICAO])
        if not icao_raw:
            return None  # ICAO is the only required identifier

        icao_hex = icao_raw.lower()

        # Prefer the generated (broadcast-time) timestamp; fall back to
        # logged (received-time); final fallback is wall clock now.
        ts = _combine_sbs_timestamp(
            _clean(fields[_SBS_GENERATED_DATE]),
            _clean(fields[_SBS_GENERATED_TIME]),
        )
        if ts is None:
            ts = _combine_sbs_timestamp(
                _clean(fields[_SBS_LOGGED_DATE]),
                _clean(fields[_SBS_LOGGED_TIME]),
            )
        if ts is None:
            ts = _now_iso()

        event: dict[str, Any] = {
            "timestamp": ts,
            "icao_hex": icao_hex,
            "callsign": _clean(fields[_SBS_CALLSIGN]),
            "altitude_ft": _try_int(_clean(fields[_SBS_ALTITUDE])),
            "ground_speed_kt": _try_int(_clean(fields[_SBS_GROUND_SPEED])),
            "track_deg": _try_float(_clean(fields[_SBS_TRACK])),
            "latitude": _try_float(_clean(fields[_SBS_LATITUDE])),
            "longitude": _try_float(_clean(fields[_SBS_LONGITUDE])),
            "vertical_rate_fpm": _try_int(_clean(fields[_SBS_VERTICAL_RATE])),
            "squawk": _clean(fields[_SBS_SQUAWK]),
            "message_type": _clean(fields[_SBS_TRANSMISSION_TYPE]),
            # RSSI is not emitted on the SBS-1 port; live capture stores
            # NULL. A future upgrade to the Beast/JSON port can populate.
            "rssi_dbfs": None,
        }

        # Extra fields bundled as JSON so the primary row stays lean.
        extra = {
            "session_id": _clean(fields[_SBS_SESSION_ID]),
            "aircraft_id": _clean(fields[_SBS_AIRCRAFT_ID]),
            "flight_id": _clean(fields[_SBS_FLIGHT_ID]),
            "alert": _try_bool_int(_clean(fields[_SBS_ALERT])),
            "emergency": _try_bool_int(_clean(fields[_SBS_EMERGENCY])),
            "spi": _try_bool_int(_clean(fields[_SBS_SPI])),
            "is_on_ground": _try_bool_int(_clean(fields[_SBS_IS_ON_GROUND])),
        }
        # Drop None-valued extras to keep the JSON compact.
        extra = {k: v for k, v in extra.items() if v is not None}
        event["extra_json"] = json.dumps(extra) if extra else None

        return event
    except Exception:
        # Unexpected parse failure (e.g. malformed unicode after decode
        # errors='replace'). Don't crash the connection — just skip.
        return None


class AdsbCaptureD(BaseCaptureD):
    """ADS-B capture daemon: TCP client for readsb's SBS-1 stream.

    Mirrors the WiFi/BT capture pattern: subclass ``BaseCaptureD``,
    implement ``_setup``/``_capture``/``_teardown``. The base class
    handles the ingest-bus connection, signal handling, and the
    ``source`` field on emitted events (auto-set to ``self.name``).
    """

    @property
    def name(self) -> str:
        return "sdr_adsb"

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__(config_path)
        self._sdr_cfg = self._cfg.sdr
        self._host: str = self._sdr_cfg.adsb_readsb_host
        self._port: int = self._sdr_cfg.adsb_readsb_port
        self._base_backoff: float = float(self._sdr_cfg.adsb_reconnect_backoff_s)
        self._writer: asyncio.StreamWriter | None = None
        self._bad_line_count = 0

    async def _setup(self) -> None:
        """No hardware to init — just validate config and log intent."""
        if not self._sdr_cfg.adsb_enabled:
            self._logger.warning("ADS-B capture disabled in config")
            raise SystemExit(0)

        if self._base_backoff <= 0:
            # Guard against a config typo pinning us in a tight loop.
            self._logger.warning(
                "adsb_reconnect_backoff_s must be > 0 (got %s) — using 2s",
                self._base_backoff,
            )
            self._base_backoff = 2.0

        self._logger.info(
            "ADS-B capture configured: readsb at %s:%d, base backoff %.1fs",
            self._host, self._port, self._base_backoff,
        )

    async def _capture(self) -> AsyncIterator[dict[str, Any]]:
        """Reconnecting SBS-1 stream reader.

        Outer loop owns the reconnect/backoff behavior. Inner loop
        reads one line at a time and yields a parsed event per MSG
        record. ConnectionRefused/Reset + generic OSError are the
        expected transient failures; we log, sleep, and retry. Other
        exceptions propagate so systemd can restart us.
        """
        backoff = self._base_backoff

        while self._running:
            try:
                reader, writer = await asyncio.open_connection(self._host, self._port)
                self._writer = writer
                self._logger.info(
                    "Connected to readsb SBS-1 at %s:%d", self._host, self._port,
                )
                backoff = self._base_backoff  # reset on successful connect

                while self._running:
                    # readline() returns b"" on EOF and can raise on
                    # broken connection. Both are treated as reconnect
                    # triggers via the except clauses below.
                    line_bytes = await reader.readline()
                    if not line_bytes:
                        self._logger.warning("readsb closed the connection (EOF)")
                        break
                    line = line_bytes.decode("ascii", errors="replace").strip()
                    if not line:
                        continue

                    event = _parse_sbs1(line)
                    if event is None:
                        self._bad_line_count += 1
                        if self._bad_line_count % _PARSE_WARN_EVERY == 1:
                            # Log the first offender and every Nth after.
                            # Truncate the line to avoid log floods from
                            # a pathological upstream.
                            self._logger.warning(
                                "Discarded %d unparseable SBS-1 line(s); "
                                "latest: %r",
                                self._bad_line_count, line[:120],
                            )
                        continue

                    yield event

            except (ConnectionRefusedError, ConnectionResetError) as exc:
                self._logger.info(
                    "readsb unreachable (%s) — retrying in %.1fs",
                    exc.__class__.__name__, backoff,
                )
            except asyncio.IncompleteReadError:
                self._logger.info(
                    "readsb stream ended mid-line — retrying in %.1fs", backoff,
                )
            except OSError as exc:
                self._logger.warning(
                    "Network error on readsb connection: %s — retrying in %.1fs",
                    exc, backoff,
                )
            finally:
                if self._writer is not None:
                    try:
                        self._writer.close()
                        # wait_closed() can raise if the peer is already
                        # gone; we don't care, we're reconnecting anyway.
                        await self._writer.wait_closed()
                    except Exception:
                        pass
                    self._writer = None

            if not self._running:
                break

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_S)

    async def _teardown(self) -> None:
        """Close the outbound connection if still open on shutdown."""
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None


if __name__ == "__main__":
    BaseCaptureD.entrypoint(AdsbCaptureD)
