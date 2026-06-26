"""433 MHz ISM-band capture daemon for Sentinel.

Spawns ``rtl_433`` as a subprocess in JSON line-output mode, reads each
decoded event off stdout, classifies it via the protocol map, and emits
one Sentinel event per decoded transmission. Events flow through the
standard ingest bus; the ingest daemon routes them to one of three
tables (sdr_tpms, sdr_weather, sdr_ism) based on a ``_target_table``
control field on the event.

Runs as root (system unit) because rtl_433 needs raw USB access to the
RTL-SDR dongle. Mirrors sentinel-wifi/sentinel-bt rather than
sentinel-sdr-adsb (which is just a TCP client to readsb).

Single-SDR coordination is operational, not enforced by systemd: the
operator stops readsb (which owns the dongle for ADS-B) and physically
swaps the antenna before flipping ``sdr.rtl433_enabled`` to true.

Usage:
    python -m sentinel.capture.sdr_433
    python -m sentinel.capture.sdr_433 --config /path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sentinel.capture.base import BaseCaptureD
from sentinel.capture.protocol_map import match_protocol

logger = logging.getLogger("sentinel.sdr_433")

# How long to wait for rtl_433 to exit gracefully on shutdown before
# escalating to SIGKILL. rtl_433 normally exits within a few hundred ms
# of SIGTERM; 5s is generous.
_PROC_SHUTDOWN_S = 5.0

# Respawn backoff: starts at 2.0s, doubles on each consecutive failure
# up to this ceiling. Mirrors adsb.py's reconnect cap.
_RESPAWN_BASE_S = 2.0
_RESPAWN_MAX_S = 60.0

# Parser-warning throttle: log at most one warning per N malformed lines
# so a flaky build of rtl_433 can't flood the journal.
_PARSE_WARN_EVERY = 100


def _now_iso() -> str:
    """Current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _freq_hz_to_mhz_string(hz: int) -> str:
    """Convert ``center_freq`` (Hz, int) to rtl_433's ``-f`` argument.

    rtl_433 accepts ``433.92M`` style strings. We render with up to 3
    decimal places of MHz (kHz precision) and trim trailing zeros so
    a clean integer MHz value renders as ``"433M"`` not ``"433.000M"``.
    """
    mhz = hz / 1_000_000.0
    # Three decimals = kHz precision, plenty for ISM-band tuning.
    formatted = f"{mhz:.3f}".rstrip("0").rstrip(".")
    return f"{formatted}M"


def _try_str(value: Any) -> str | None:
    """Coerce to string or return None for missing/empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _try_float(value: Any) -> float | None:
    """Coerce to float or return None on any failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _try_bool_int(value: Any) -> int | None:
    """rtl_433 emits 0/1 ints for boolean flags. Pass through; coerce
    truthy strings; return None on anything else."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(bool(value))
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("0", "false", "no", ""):
            return 0
        if v in ("1", "true", "yes"):
            return 1
    return None


def _battery_low_from_ok(raw: dict[str, Any]) -> int | None:
    """rtl_433 emits ``battery_ok`` (truthy = good battery); our schema
    stores ``battery_low`` (truthy = low). Invert. None passes through.
    """
    battery_ok = _try_bool_int(raw.get("battery_ok"))
    if battery_ok is None:
        return None
    return 1 - battery_ok


def _route_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a raw rtl_433 JSON event into a Sentinel event dict.

    Returns None only on unrecoverable shape problems (currently never —
    every shape lands somewhere, even if just sdr_ism/unknown). Never
    raises: malformed numeric fields become None rather than breaking
    the row.

    The returned dict carries a ``_target_table`` key the ingest side
    uses to dispatch into the right buffer. The base class will set
    ``source`` to ``self.name`` ("sdr_433") regardless — the target
    table is the finer-grained classification.

    Timestamp policy: rtl_433 emits a local-time string without a
    timezone suffix; reinterpreting that as UTC would silently corrupt
    timestamps on any non-UTC Pi. We use wall-clock now (correct to
    within decoder latency) and preserve the raw ``time`` field inside
    extra_json for forensic comparison.
    """
    model = _try_str(raw.get("model"))
    target_table, category = match_protocol(model)

    timestamp = _now_iso()
    rssi = _try_float(raw.get("rssi"))

    # Every event carries the full raw payload in extra_json so we can
    # refine downstream parsing without re-capturing. rtl_433's own
    # "time" string is preserved in there for forensic use.
    extra_json = json.dumps(raw, sort_keys=True, default=str)

    if target_table == "sdr_tpms":
        # rtl_433 TPMS decoders use ``id`` for the sensor ID. Some emit
        # it as int, some as hex string — coerce to string either way.
        sensor_id = _try_str(raw.get("id"))
        if not sensor_id:
            # No identifier → not useful for cross-source correlation.
            # Drop to ISM with category 'tpms_no_id' so we still capture.
            return {
                "_target_table": "sdr_ism",
                "timestamp": timestamp,
                "device_id": None,
                "protocol": model,
                "category": "tpms_no_id",
                "rssi": rssi,
                "extra_json": extra_json,
            }
        return {
            "_target_table": "sdr_tpms",
            "timestamp": timestamp,
            "sensor_id": sensor_id,
            "protocol": model,
            "pressure_kpa": _try_float(raw.get("pressure_kPa")),
            "temperature_c": _try_float(raw.get("temperature_C")),
            "battery_low": _battery_low_from_ok(raw),
            "rssi": rssi,
            "flags": _try_str(raw.get("flags")),
            "extra_json": extra_json,
        }

    if target_table == "sdr_weather":
        station_id = _try_str(raw.get("id"))
        if not station_id:
            return {
                "_target_table": "sdr_ism",
                "timestamp": timestamp,
                "device_id": None,
                "protocol": model,
                "category": "weather_no_id",
                "rssi": rssi,
                "extra_json": extra_json,
            }
        return {
            "_target_table": "sdr_weather",
            "timestamp": timestamp,
            "station_id": station_id,
            "protocol": model,
            "temperature_c": _try_float(raw.get("temperature_C")),
            "humidity": _try_float(raw.get("humidity")),
            "wind_kph": _try_float(raw.get("wind_avg_km_h")),
            "rain_mm": _try_float(raw.get("rain_mm")),
            "battery_low": _battery_low_from_ok(raw),
            "rssi": rssi,
            "extra_json": extra_json,
        }

    # sdr_ism (catch-all)
    return {
        "_target_table": "sdr_ism",
        "timestamp": timestamp,
        "device_id": _try_str(raw.get("id")),
        "protocol": model,
        "category": category or "unknown",
        "rssi": rssi,
        "extra_json": extra_json,
    }


class Sdr433CaptureD(BaseCaptureD):
    """433 MHz capture daemon: rtl_433 subprocess JSON-stream reader.

    Mirrors AdsbCaptureD's structure (subclass BaseCaptureD, implement
    setup/capture/teardown) but the upstream is a child process rather
    than a TCP socket. Same respawn-with-backoff pattern: if rtl_433
    crashes, we respawn after a backoff that doubles up to 60s.
    """

    @property
    def name(self) -> str:
        return "sdr_433"

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__(config_path)
        self._sdr_cfg = self._cfg.sdr
        self._freq_arg: str = _freq_hz_to_mhz_string(
            int(self._sdr_cfg.center_freq)
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._bad_line_count = 0

    async def _setup(self) -> None:
        """Validate config; refuse to start unless explicitly enabled."""
        if not getattr(self._sdr_cfg, "rtl433_enabled", False):
            self._logger.warning("rtl_433 capture disabled in config")
            raise SystemExit(0)
        self._logger.info(
            "rtl_433 capture configured: freq=%s (operator must ensure "
            "readsb is stopped and antenna is swapped to 433 MHz)",
            self._freq_arg,
        )

    async def _capture(self) -> AsyncIterator[dict[str, Any]]:
        """Respawning rtl_433 subprocess reader.

        Outer loop owns respawn/backoff. Inner loop reads one JSON line
        at a time, routes via the protocol map, yields a shaped event.
        Parse errors are throttled-logged and skipped; the subprocess is
        only torn down on EOF or explicit shutdown.
        """
        backoff = _RESPAWN_BASE_S

        while self._running:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    "rtl_433", "-F", "json", "-M", "level", "-f", self._freq_arg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._logger.info(
                    "Spawned rtl_433 (pid=%d) on %s",
                    self._proc.pid, self._freq_arg,
                )
                backoff = _RESPAWN_BASE_S  # reset on successful spawn

                assert self._proc.stdout is not None
                while self._running:
                    line_bytes = await self._proc.stdout.readline()
                    if not line_bytes:
                        # rtl_433 exited (EOF on stdout). Check return
                        # code so the log explains what happened.
                        rc = await self._proc.wait()
                        self._logger.warning(
                            "rtl_433 exited (rc=%s) — respawning", rc,
                        )
                        break

                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        self._bad_line_count += 1
                        if self._bad_line_count % _PARSE_WARN_EVERY == 1:
                            self._logger.warning(
                                "Discarded %d non-JSON line(s) from "
                                "rtl_433; latest: %r",
                                self._bad_line_count, line[:200],
                            )
                        continue

                    event = _route_event(raw)
                    if event is None:
                        continue
                    yield event

            except FileNotFoundError:
                # rtl_433 binary missing — no point in respawning.
                self._logger.error(
                    "rtl_433 binary not found in PATH. Install with: "
                    "sudo apt install rtl-433"
                )
                raise SystemExit(1)
            except Exception as exc:
                self._logger.warning(
                    "rtl_433 subprocess error: %s — respawning in %.1fs",
                    exc, backoff,
                )
            finally:
                await self._kill_proc()

            if not self._running:
                break

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RESPAWN_MAX_S)

    async def _teardown(self) -> None:
        """Kill the rtl_433 subprocess if still running."""
        await self._kill_proc()

    async def _kill_proc(self) -> None:
        """Send SIGTERM, wait briefly, escalate to SIGKILL."""
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_PROC_SHUTDOWN_S)
        except asyncio.TimeoutError:
            self._logger.warning(
                "rtl_433 didn't exit within %.1fs — sending SIGKILL",
                _PROC_SHUTDOWN_S,
            )
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    BaseCaptureD.entrypoint(Sdr433CaptureD)
