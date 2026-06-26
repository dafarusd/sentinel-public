"""Bluetooth capture daemon for Sentinel.

BLE scanning via bleak (continuous, callback-based).
Classic BT inquiry via dbus-next talking directly to BlueZ over D-Bus.

Emits normalized events to the ingest bus with:
    timestamp, mac, device_name, rssi, tx_power, manufacturer_data_hex,
    service_uuids, device_class, is_classic

Requires: Bluetooth adapter (Pi 5 built-in hci0), bluetoothd running.

Usage:
    python -m sentinel.capture.bluetooth
    python -m sentinel.capture.bluetooth --config /path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sentinel.capture.base import BaseCaptureD
from sentinel.config import get_config
from sentinel.profiler.ble_fingerprint import compute_mfr_fingerprint

logger = logging.getLogger("sentinel.bt")

# BlueZ D-Bus constants
BLUEZ_SERVICE = "org.bluez"
BLUEZ_ADAPTER_IFACE = "org.bluez.Adapter1"
BLUEZ_DEVICE_IFACE = "org.bluez.Device1"
DBUS_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
DBUS_OBJECTMANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"


def _now_iso() -> str:
    """Current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _format_manufacturer_data(mfr_data: dict[int, bytes] | None) -> str | None:
    """Format manufacturer data as hex string.

    bleak returns {company_id: bytes}, we flatten to hex.
    """
    if not mfr_data:
        return None
    parts = []
    for company_id, data in mfr_data.items():
        parts.append(f"{company_id:04x}{data.hex()}")
    return "".join(parts) if parts else None


class BleScanner:
    """BLE advertisement scanner using bleak.

    Runs continuously and pushes detected advertisements into an asyncio queue.
    """

    def __init__(self, adapter: str, event_queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._adapter = adapter
        self._queue = event_queue
        self._scanner: Any = None  # BleakScanner instance
        # Stage 16: watchdog state for silent-failure recovery
        self._last_event_ts: float = 0.0
        self._watchdog_task: asyncio.Task[None] | None = None
        self._watchdog_running: bool = False
        # Stage 16b: rolling event-rate tracking for degradation detection.
        # Track count of events per minute over the last 5 minutes; deque
        # auto-evicts old buckets beyond maxlen.
        self._rate_buckets: deque[tuple[float, int]] = deque(maxlen=5)
        self._current_bucket_start: float = 0.0
        self._current_bucket_count: int = 0
        self._baseline_rate_per_min: float = 0.0  # learned rolling baseline

    def _detection_callback(self, device: Any, advertisement_data: Any) -> None:
        """Called by bleak for each BLE advertisement."""
        try:
            # Stage 16b: heartbeat + rate tracking for degradation watchdog.
            # Buckets are 60-second windows; deque auto-evicts beyond maxlen.
            # Updated before any processing so even malformed advertisements
            # count as proof the scanner is alive.
            now = time.monotonic()
            self._last_event_ts = now
            if now - self._current_bucket_start >= 60.0:
                if self._current_bucket_start > 0.0:
                    # Save the completed bucket
                    self._rate_buckets.append(
                        (self._current_bucket_start, self._current_bucket_count)
                    )
                self._current_bucket_start = now
                self._current_bucket_count = 0
            self._current_bucket_count += 1
            mfr_hex = _format_manufacturer_data(advertisement_data.manufacturer_data)
            uuids = (
                list(advertisement_data.service_uuids)
                if advertisement_data.service_uuids
                else []
            )
            event = {
                "timestamp": _now_iso(),
                "mac": device.address.lower(),
                "device_name": advertisement_data.local_name,
                "rssi": advertisement_data.rssi,
                "tx_power": advertisement_data.tx_power,
                "manufacturer_data_hex": mfr_hex,
                "service_uuids": uuids,
                "device_class": None,  # BLE doesn't have CoD
                "is_classic": False,
            }
            # Stage 14d: canonical mfr-data fingerprint. None when the
            # advertisement has no fingerprintable structure — stored as
            # NULL downstream. Hashing the same sorted-UUIDs JSON we emit
            # keeps fingerprint stable between live capture and backfill.
            fp = compute_mfr_fingerprint(
                mfr_hex,
                json.dumps(sorted(uuids)) if uuids else None,
            )
            if fp is not None:
                event["mfr_fingerprint_hash"] = fp
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop rather than block
        except Exception:
            logger.exception("Error processing BLE advertisement")

    async def start(self) -> None:
        """Start the BLE scanner."""
        from bleak import BleakScanner

        # bleak on Linux uses BlueZ adapter name like "hci0"
        self._scanner = BleakScanner(
            detection_callback=self._detection_callback,
            scanning_mode="active",
            bluez={"adapter": self._adapter},
        )
        await self._scanner.start()
        logger.info("BLE scanner started on %s", self._adapter)

        # Stage 16: launch watchdog as background task
        self._watchdog_running = True
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        """Stop the BLE scanner."""
        # Stage 16: stop watchdog cleanly before tearing down the scanner
        # so it can't race against our stop() and try to restart mid-shutdown.
        self._watchdog_running = False
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None

        if self._scanner is not None:
            try:
                await self._scanner.stop()
            except Exception:
                logger.exception("Error stopping BLE scanner")
            self._scanner = None
            logger.info("BLE scanner stopped")

    async def _watchdog_loop(self) -> None:
        """Monitor scanner health: silent failure AND degraded-rate detection.

        Stage 16b: BleakScanner can return start() success but enter degraded
        states under heavy BLE density on Pi 5 (bluez/bluez#904, #1500). The
        scanner's underlying bluez session loses advertisement reports without
        any error surfacing. This loop watches both:

        1. SILENT failure: no events for 90s+ -> attempt restart
        2. DEGRADED rate: events flow but at <30% of learned baseline for 3+
           consecutive minutes -> attempt restart

        Recovery escalation:
        - First detected failure (silent OR degraded): log warning, restart
          BleakScanner, give 60s grace period
        - Second consecutive failure post-restart: log CRITICAL, back off.
          Per bluez/bluez#904 only 'systemctl restart bluetooth' reliably
          recovers from this state. Operator action required.
        """
        SILENT_THRESHOLD_S = 90.0
        DEGRADED_THRESHOLD_RATIO = 0.30  # below 30% of baseline = degraded
        DEGRADED_MIN_MINUTES = 3          # require 3 consecutive low buckets
        BASELINE_LEARNING_MIN_BUCKETS = 3 # need 3 healthy buckets to learn baseline
        CHECK_INTERVAL_S = 30.0
        POST_RESTART_GRACE_S = 60.0

        logger.info(
            "BLE watchdog started (silent=%.0fs, degraded=<%.0f%% of baseline x %d min)",
            SILENT_THRESHOLD_S,
            DEGRADED_THRESHOLD_RATIO * 100,
            DEGRADED_MIN_MINUTES,
        )

        # Initial seed: don't fire watchdog before any events have a chance to arrive
        self._last_event_ts = time.monotonic()
        restart_attempted = False
        post_restart_until: float = 0.0

        while self._watchdog_running:
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)

                if not self._watchdog_running:
                    break

                now = time.monotonic()
                idle_s = now - self._last_event_ts

                # During post-restart grace period, only check for full silence
                in_grace = now < post_restart_until

                # Update baseline from healthy buckets (excluding current incomplete one)
                if len(self._rate_buckets) >= BASELINE_LEARNING_MIN_BUCKETS:
                    rates = [count for (_ts, count) in self._rate_buckets]
                    rates.sort()
                    # Use median to avoid skew from one anomalous bucket
                    median_rate = rates[len(rates) // 2]
                    if median_rate > 0:
                        # Slowly adapt baseline upward, react quickly downward
                        # for now just track the recent median
                        self._baseline_rate_per_min = float(median_rate)

                # Check 1: full silence (always fires regardless of grace)
                silent = idle_s >= SILENT_THRESHOLD_S

                # Check 2: degraded rate (only after baseline learned, only outside grace)
                degraded = False
                if (
                    not in_grace
                    and self._baseline_rate_per_min > 5.0  # don't trip on naturally quiet env
                    and len(self._rate_buckets) >= DEGRADED_MIN_MINUTES
                ):
                    threshold = self._baseline_rate_per_min * DEGRADED_THRESHOLD_RATIO
                    recent = list(self._rate_buckets)[-DEGRADED_MIN_MINUTES:]
                    if all(count < threshold for (_ts, count) in recent):
                        degraded = True

                if not silent and not degraded:
                    # Healthy: reset restart flag if we previously attempted recovery
                    if restart_attempted:
                        logger.info(
                            "BLE watchdog: scanner recovered "
                            "(idle=%.0fs, baseline=%.0f/min)",
                            idle_s, self._baseline_rate_per_min,
                        )
                        restart_attempted = False
                    continue

                # Failure detected
                failure_kind = "silent" if silent else "degraded"

                if not restart_attempted:
                    if silent:
                        logger.warning(
                            "BLE watchdog: SILENT failure - no events for %.0fs "
                            "(threshold %.0fs). Attempting scanner restart.",
                            idle_s, SILENT_THRESHOLD_S,
                        )
                    else:
                        recent_rates = [count for (_ts, count) in
                                        list(self._rate_buckets)[-DEGRADED_MIN_MINUTES:]]
                        logger.warning(
                            "BLE watchdog: DEGRADED rate - last %d minute(s) %s "
                            "vs baseline %.0f/min (threshold %.1f). "
                            "Attempting scanner restart.",
                            DEGRADED_MIN_MINUTES,
                            recent_rates,
                            self._baseline_rate_per_min,
                            self._baseline_rate_per_min * DEGRADED_THRESHOLD_RATIO,
                        )

                    try:
                        if self._scanner is not None:
                            await self._scanner.stop()
                        await asyncio.sleep(2.0)
                        from bleak import BleakScanner
                        self._scanner = BleakScanner(
                            detection_callback=self._detection_callback,
                            scanning_mode="active",
                            bluez={"adapter": self._adapter},
                        )
                        await self._scanner.start()
                        logger.info(
                            "BLE watchdog: scanner restarted on %s, %.0fs grace period",
                            self._adapter, POST_RESTART_GRACE_S,
                        )
                        # Reset rate tracking after restart
                        self._rate_buckets.clear()
                        self._current_bucket_start = time.monotonic()
                        self._current_bucket_count = 0
                        self._last_event_ts = time.monotonic()
                        post_restart_until = time.monotonic() + POST_RESTART_GRACE_S
                        restart_attempted = True
                    except Exception:
                        logger.exception(
                            "BLE watchdog: scanner restart failed. "
                            "Manual intervention required."
                        )
                        restart_attempted = True
                else:
                    # Already attempted restart, still failing. Escalate.
                    logger.critical(
                        "BLE watchdog: %s state PERSISTS after restart "
                        "(idle=%.0fs, baseline=%.0f/min). "
                        "Per bluez/bluez#904: only 'sudo systemctl restart bluetooth' "
                        "reliably recovers from this state on Pi 5. "
                        "If this happens often, sentinel-bt-recovery.timer will "
                        "restart bluetoothd every 4 hours preventively. "
                        "For immediate recovery: sudo systemctl restart bluetooth "
                        "&& sudo systemctl restart sentinel-bt. "
                        "Watchdog backing off to avoid hammering bluez.",
                        failure_kind, idle_s, self._baseline_rate_per_min,
                    )
                    self._watchdog_running = False
                    break

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("BLE watchdog: unexpected error in loop")
                await asyncio.sleep(CHECK_INTERVAL_S)

        logger.info("BLE watchdog stopped")


class ClassicBtScanner:
    """Classic Bluetooth device discovery via BlueZ D-Bus API.

    Runs periodic inquiry cycles using org.bluez.Adapter1.StartDiscovery,
    collecting device properties from PropertiesChanged signals.
    """

    def __init__(
        self,
        adapter: str,
        event_queue: asyncio.Queue[dict[str, Any]],
        inquiry_duration_s: int = 8,
        inquiry_interval_s: int = 60,
    ) -> None:
        self._adapter_name = adapter
        self._adapter_path = f"/org/bluez/{adapter}"
        self._queue = event_queue
        self._inquiry_duration = inquiry_duration_s
        self._inquiry_interval = inquiry_interval_s
        self._bus: Any = None
        self._running = False
        self._seen_devices: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        """Connect to D-Bus and start discovery cycles."""
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()  # system bus = 2
        self._running = True

        # Subscribe to InterfacesAdded for new device objects
        self._bus.add_message_handler(self._handle_signal)

        logger.info("Classic BT scanner connected to D-Bus, adapter: %s", self._adapter_name)

    async def run_inquiry_cycle(self) -> None:
        """Run one discovery cycle: start, wait, stop, emit events."""
        if self._bus is None:
            return

        try:
            # Get adapter proxy
            introspection = await self._bus.introspect(BLUEZ_SERVICE, self._adapter_path)
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, self._adapter_path, introspection
            )
            adapter = proxy.get_interface(BLUEZ_ADAPTER_IFACE)

            # Start discovery
            await adapter.call_start_discovery()
            logger.debug("Classic BT discovery started")

            await asyncio.sleep(self._inquiry_duration)

            # Stop discovery
            await adapter.call_stop_discovery()
            logger.debug("Classic BT discovery stopped")

            # Collect discovered devices from BlueZ object tree
            await self._collect_devices()

        except Exception:
            logger.exception("Classic BT inquiry cycle error")

    async def run_loop(self) -> None:
        """Run periodic inquiry cycles until stopped."""
        while self._running:
            await self.run_inquiry_cycle()
            # Wait before next cycle, but check _running periodically
            for _ in range(int(self._inquiry_interval)):
                if not self._running:
                    break
                await asyncio.sleep(1.0)

    async def _collect_devices(self) -> None:
        """Query BlueZ for all discovered device objects and emit events."""
        if self._bus is None:
            return

        try:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, "/")
            proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
            obj_manager = proxy.get_interface(DBUS_OBJECTMANAGER_IFACE)

            objects = await obj_manager.call_get_managed_objects()

            for path, interfaces in objects.items():
                if BLUEZ_DEVICE_IFACE not in interfaces:
                    continue

                # Only devices under our adapter
                if not path.startswith(self._adapter_path + "/"):
                    continue

                props = interfaces[BLUEZ_DEVICE_IFACE]
                self._emit_device(props)

        except Exception:
            logger.exception("Error collecting classic BT devices")

    def _handle_signal(self, msg: Any) -> bool:
        """Handle D-Bus signals for device property changes."""
        from dbus_next import MessageType

        if msg.message_type != MessageType.SIGNAL:
            return False

        # InterfacesAdded on ObjectManager
        if (
            msg.member == "InterfacesAdded"
            and msg.body
            and len(msg.body) >= 2
        ):
            path = msg.body[0]
            interfaces = msg.body[1]
            if BLUEZ_DEVICE_IFACE in interfaces:
                props = interfaces[BLUEZ_DEVICE_IFACE]
                self._emit_device(props)
            return False

        # PropertiesChanged on a device
        if (
            msg.member == "PropertiesChanged"
            and msg.body
            and len(msg.body) >= 2
            and msg.body[0] == BLUEZ_DEVICE_IFACE
        ):
            changed_props = msg.body[1]
            # Extract address from object path: /org/bluez/hci0/dev_XX_XX_XX_XX_XX_XX
            if msg.path and "/dev_" in msg.path:
                addr = msg.path.split("/dev_")[-1].replace("_", ":").lower()
                # Merge with previously seen props
                if addr in self._seen_devices:
                    self._seen_devices[addr].update(self._unpack_variants(changed_props))
                else:
                    self._seen_devices[addr] = self._unpack_variants(changed_props)

                if "RSSI" in changed_props:
                    self._emit_device(self._seen_devices[addr])
            return False

        return False

    def _emit_device(self, props: dict[str, Any]) -> None:
        """Convert BlueZ device properties to a Sentinel event and enqueue."""
        try:
            unpacked = self._unpack_variants(props)

            address = unpacked.get("Address", "")
            if not address:
                return

            mac = address.lower()

            # Update seen cache
            self._seen_devices[mac] = unpacked

            rssi = unpacked.get("RSSI")
            if rssi is None:
                return  # No RSSI = stale cached device, skip

            # Extract manufacturer data
            mfr_hex = None
            mfr_data = unpacked.get("ManufacturerData")
            if mfr_data and isinstance(mfr_data, dict):
                parts = []
                for company_id, data_bytes in mfr_data.items():
                    if isinstance(data_bytes, (bytes, bytearray)):
                        parts.append(f"{company_id:04x}{data_bytes.hex()}")
                    elif hasattr(data_bytes, "value"):
                        # dbus Variant wrapper
                        parts.append(f"{company_id:04x}{bytes(data_bytes.value).hex()}")
                mfr_hex = "".join(parts) if parts else None

            # Service UUIDs
            uuids = unpacked.get("UUIDs", [])
            if isinstance(uuids, list):
                uuids = [str(u) for u in uuids]
            else:
                uuids = []

            event: dict[str, Any] = {
                "timestamp": _now_iso(),
                "mac": mac,
                "device_name": unpacked.get("Name") or unpacked.get("Alias"),
                "rssi": int(rssi) if rssi is not None else None,
                "tx_power": unpacked.get("TxPower"),
                "manufacturer_data_hex": mfr_hex,
                "service_uuids": uuids,
                "device_class": unpacked.get("Class"),
                "is_classic": True,
            }
            # Stage 14d: same canonical mfr-data fingerprint as the BLE
            # path. Classic BT rarely carries manufacturer data, but when
            # it does the fingerprint is transport-agnostic.
            fp = compute_mfr_fingerprint(
                mfr_hex,
                json.dumps(sorted(uuids)) if uuids else None,
            )
            if fp is not None:
                event["mfr_fingerprint_hash"] = fp

            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        except Exception:
            logger.exception("Error emitting classic BT device event")

    @staticmethod
    def _unpack_variants(props: dict[str, Any]) -> dict[str, Any]:
        """Unpack dbus-next Variant wrappers from a properties dict."""
        from dbus_next import Variant

        result: dict[str, Any] = {}
        for key, val in props.items():
            if isinstance(val, Variant):
                result[key] = val.value
            else:
                result[key] = val
        return result

    async def stop(self) -> None:
        """Stop discovery and disconnect from D-Bus."""
        self._running = False
        if self._bus is not None:
            try:
                # Try to stop discovery if running
                introspection = await self._bus.introspect(BLUEZ_SERVICE, self._adapter_path)
                proxy = self._bus.get_proxy_object(
                    BLUEZ_SERVICE, self._adapter_path, introspection
                )
                adapter = proxy.get_interface(BLUEZ_ADAPTER_IFACE)
                await adapter.call_stop_discovery()
            except Exception:
                pass  # May already be stopped
            self._bus.disconnect()
            self._bus = None
            logger.info("Classic BT scanner stopped")


class BluetoothCaptureD(BaseCaptureD):
    """Bluetooth capture daemon — BLE + Classic BT."""

    @property
    def name(self) -> str:
        return "bt"

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__(config_path)
        self._bt_cfg = self._cfg.bluetooth
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5_000)
        self._ble_scanner: BleScanner | None = None
        self._classic_scanner: ClassicBtScanner | None = None
        self._classic_task: asyncio.Task[None] | None = None

    async def _setup(self) -> None:
        """Initialize BLE and classic BT scanners."""
        if not self._bt_cfg.enabled:
            self._logger.warning("Bluetooth capture disabled in config")
            raise SystemExit(0)

        # BLE scanner
        self._ble_scanner = BleScanner(
            adapter=self._bt_cfg.adapter,
            event_queue=self._event_queue,
        )
        try:
            await self._ble_scanner.start()
        except Exception:
            self._logger.exception("BLE scanner failed to start — continuing with classic only")
            self._ble_scanner = None

        # Classic BT scanner
        self._classic_scanner = ClassicBtScanner(
            adapter=self._bt_cfg.adapter,
            event_queue=self._event_queue,
            inquiry_duration_s=self._bt_cfg.classic_inquiry_duration_s,
            inquiry_interval_s=self._bt_cfg.classic_inquiry_interval_s,
        )
        try:
            await self._classic_scanner.start()
            # Run classic inquiry loop as a background task
            self._classic_task = asyncio.create_task(self._classic_scanner.run_loop())
        except Exception:
            self._logger.exception(
                "Classic BT scanner failed to start — continuing with BLE only"
            )
            self._classic_scanner = None

        if self._ble_scanner is None and self._classic_scanner is None:
            raise RuntimeError("Both BLE and classic BT scanners failed to start")

    async def _capture(self) -> AsyncIterator[dict[str, Any]]:
        """Yield BT events from the shared queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    async def _teardown(self) -> None:
        """Stop all scanners."""
        if self._classic_task is not None:
            self._classic_task.cancel()
            try:
                await self._classic_task
            except asyncio.CancelledError:
                pass

        if self._classic_scanner is not None:
            await self._classic_scanner.stop()

        if self._ble_scanner is not None:
            await self._ble_scanner.stop()


if __name__ == "__main__":
    BaseCaptureD.entrypoint(BluetoothCaptureD)
