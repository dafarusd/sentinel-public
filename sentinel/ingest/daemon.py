"""Ingestion daemon for Sentinel.

Runs the IPC bus server, receives events from all capture daemons,
enriches, deduplicates, and batch-writes to the SQLite database.

Pipeline per event:
    1. Validate — check required fields (timestamp, mac, source)
    2. Enrich   — OUI vendor lookup, GPS location join, device type inference
    3. Dedup    — skip if same (mac, channel) seen within dedup window
    4. Buffer   — accumulate in batch
    5. Flush    — write batch to DB (every batch_interval_s or batch_max_events)

Writes to: observations, wifi_frames, probe_requests, bt_advertisements, devices.

Usage:
    python -m sentinel.ingest.daemon
    python -m sentinel.ingest.daemon --config /path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentinel.common.oui import is_locally_administered, lookup_vendor
from sentinel.config import (
    get_config,
    install_sighup_handler,
    load_config,
    reload_thresholds,
)
from sentinel.db.schema import apply_schema
from sentinel.db.writer import DatabaseWriter
from sentinel.identity.loader import load_identity_map, lookup_identity
from sentinel.ingest.bus import BusServer

logger = logging.getLogger("sentinel.ingest")

# WiFi frame subtypes that indicate an AP
_AP_SUBTYPES = {8, 5}  # beacon, probe response

# Probe request subtype
_SUBTYPE_PROBE_REQ = 4

# Sources that don't have a MAC (aircraft, ISM emitters). They bypass
# enrich_event (OUI lookup is mac-keyed) and the dedup table (also
# mac-keyed). EventBatcher routes them by source — and for sdr_433, by
# the ``_target_table`` control field set in the capture daemon, since
# all three sub-streams (tpms / weather / ism) share the same source.
NON_MAC_SOURCES: frozenset[str] = frozenset({
    "sdr_adsb", "sdr_433",
})


class Deduplicator:
    """Time-windowed deduplication for (mac, channel) pairs.

    An event is considered a duplicate if the same (mac, channel) was
    seen within dedup_window_s seconds.
    """

    def __init__(self, window_s: float = 1.0) -> None:
        self._window = window_s
        self._seen: dict[tuple[str, int | None], float] = {}
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 30.0  # purge stale entries every 30s

    def is_duplicate(self, mac: str, channel: int | None) -> bool:
        """Check if this (mac, channel) was seen recently.

        Returns True if duplicate (should be skipped).
        """
        now = time.monotonic()
        key = (mac, channel)

        # Periodic cleanup of stale entries
        if now - self._last_cleanup > self._cleanup_interval:
            self._cleanup(now)

        last_seen = self._seen.get(key)
        if last_seen is not None and (now - last_seen) < self._window:
            return True

        self._seen[key] = now
        return False

    def _cleanup(self, now: float) -> None:
        """Remove entries older than the dedup window."""
        cutoff = now - self._window
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._last_cleanup = now

    @property
    def tracked_count(self) -> int:
        """Number of (mac, channel) pairs currently tracked."""
        return len(self._seen)


class EventBatcher:
    """Accumulates events and flushes them in batches.

    Flush triggers:
        - batch_max_events reached
        - batch_interval_s elapsed since last flush
    """

    def __init__(
        self,
        writer: DatabaseWriter,
        batch_interval_s: float = 2.0,
        batch_max_events: int = 500,
    ) -> None:
        self._writer = writer
        self._interval = batch_interval_s
        self._max_events = batch_max_events

        # Buffered rows by table
        self._observations: list[tuple[Any, ...]] = []
        self._wifi_frames: list[tuple[Any, ...]] = []
        self._probe_requests: list[tuple[Any, ...]] = []
        self._bt_advertisements: list[tuple[Any, ...]] = []
        self._sdr_adsb: list[tuple[Any, ...]] = []
        self._sdr_tpms: list[tuple[Any, ...]] = []
        self._sdr_weather: list[tuple[Any, ...]] = []
        self._sdr_ism: list[tuple[Any, ...]] = []
        self._device_upserts: dict[str, dict[str, Any]] = {}  # mac -> fields

        self._last_flush = time.monotonic()
        self._total_flushed = 0

    def add(self, event: dict[str, Any]) -> None:
        """Add an enriched event to the batch buffers."""
        source = event.get("source", "unknown")
        timestamp = event["timestamp"]

        # Stage 18b: ADS-B events have no MAC and don't participate in
        # the devices / observations pipeline — aircraft live in their
        # own namespace keyed by icao_hex. Route to sdr_adsb and bail
        # out before the mac-keyed path below.
        if source == "sdr_adsb":
            self._sdr_adsb.append((
                timestamp,
                event.get("icao_hex"),
                event.get("callsign"),
                event.get("altitude_ft"),
                event.get("ground_speed_kt"),
                event.get("track_deg"),
                event.get("latitude"),
                event.get("longitude"),
                event.get("vertical_rate_fpm"),
                event.get("squawk"),
                event.get("rssi_dbfs"),
                event.get("message_type"),
                event.get("extra_json"),
            ))
            return

        # Stage D: rtl_433 events. Single source, three target tables —
        # the capture daemon stamps each event with ``_target_table``
        # based on the protocol-map lookup. Like sdr_adsb, these never
        # touch the mac/observations/devices pipeline.
        if source == "sdr_433":
            target = event.get("_target_table")
            if target == "sdr_tpms":
                self._sdr_tpms.append((
                    timestamp,
                    event.get("sensor_id"),
                    event.get("protocol"),
                    event.get("pressure_kpa"),
                    event.get("temperature_c"),
                    event.get("battery_low"),
                    event.get("rssi"),
                    event.get("flags"),
                    event.get("identity_id"),
                    event.get("extra_json"),
                ))
            elif target == "sdr_weather":
                self._sdr_weather.append((
                    timestamp,
                    event.get("station_id"),
                    event.get("protocol"),
                    event.get("temperature_c"),
                    event.get("humidity"),
                    event.get("wind_kph"),
                    event.get("rain_mm"),
                    event.get("battery_low"),
                    event.get("rssi"),
                    event.get("identity_id"),
                    event.get("extra_json"),
                ))
            elif target == "sdr_ism":
                self._sdr_ism.append((
                    timestamp,
                    event.get("device_id"),
                    event.get("protocol"),
                    event.get("category"),
                    event.get("rssi"),
                    event.get("identity_id"),
                    event.get("extra_json"),
                ))
            else:
                # Broken contract: capture daemon must always set
                # _target_table for sdr_433 events. Loud per Stage C
                # principle — silent data loss is the worse failure.
                logger.warning(
                    "sdr_433 event with missing/unknown _target_table=%r "
                    "(protocol=%r) — dropped",
                    target, event.get("protocol"),
                )
            return

        mac = event["mac"]

        # --- observations (always) ---
        self._observations.append((
            timestamp,
            mac,
            source,
            event.get("rssi"),
            event.get("channel"),
            event.get("latitude"),
            event.get("longitude"),
            event.get("identity_id"),
            event.get("extra_json"),
        ))

        # --- source-specific tables ---
        if source == "wifi":
            self._wifi_frames.append((
                timestamp,
                event.get("src_mac", mac),
                event.get("dst_mac"),
                event.get("bssid"),
                event.get("ssid"),
                event.get("channel"),
                event.get("rssi"),
                event.get("frame_type", 0),
                event.get("frame_subtype", 0),
                event.get("sequence_num"),
                event.get("identity_id"),
                None,  # extra_json
            ))

            # Probe requests get their own table
            if event.get("frame_subtype") == _SUBTYPE_PROBE_REQ:
                ie_blob = None
                if event.get("ie_bytes_hex"):
                    try:
                        ie_blob = bytes.fromhex(event["ie_bytes_hex"])
                    except (ValueError, TypeError):
                        pass
                self._probe_requests.append((
                    timestamp,
                    mac,
                    event.get("ssid"),
                    event.get("rssi"),
                    event.get("channel"),
                    event.get("sequence_num"),
                    ie_blob,
                    event.get("ie_fingerprint_hash"),
                    event.get("identity_id"),
                    None,  # extra_json
                ))

        elif source == "bt":
            service_uuids_json = None
            if event.get("service_uuids"):
                service_uuids_json = json.dumps(event["service_uuids"])
            self._bt_advertisements.append((
                timestamp,
                mac,
                event.get("device_name"),
                event.get("rssi"),
                event.get("tx_power"),
                event.get("manufacturer_data_hex"),
                service_uuids_json,
                event.get("device_class"),
                1 if event.get("is_classic") else 0,
                event.get("mfr_fingerprint_hash"),
                event.get("identity_id"),
                None,  # extra_json
            ))

        # --- device upsert ---
        existing = self._device_upserts.get(mac)
        if existing is None:
            self._device_upserts[mac] = {
                "first_seen": timestamp,
                "last_seen": timestamp,
                "vendor": event.get("vendor"),
                "device_name": event.get("device_name"),
                "device_type": event.get("device_type", "unknown"),
                "is_ap": 1 if event.get("is_ap") else 0,
                "identity_id": event.get("identity_id"),
            }
        else:
            existing["last_seen"] = timestamp
            if event.get("device_name") and not existing.get("device_name"):
                existing["device_name"] = event["device_name"]
            if event.get("is_ap"):
                existing["is_ap"] = 1
            # Stage 15: backfill identity_id if a later event has one
            # but the earlier event in this batch did not.
            if event.get("identity_id") and not existing.get("identity_id"):
                existing["identity_id"] = event["identity_id"]

    def should_flush(self) -> bool:
        """Check if a flush is due.

        Counts rows across every buffer — not just observations — so that
        single-source workloads (e.g., ADS-B-only early in the pipeline)
        still trigger a flush.
        """
        total = (
            len(self._observations)
            + len(self._wifi_frames)
            + len(self._probe_requests)
            + len(self._bt_advertisements)
            + len(self._sdr_adsb)
            + len(self._sdr_tpms)
            + len(self._sdr_weather)
            + len(self._sdr_ism)
        )
        if total >= self._max_events:
            return True
        if total > 0 and (time.monotonic() - self._last_flush) >= self._interval:
            return True
        return False

    def flush(self) -> int:
        """Write all buffered data to the database.

        Returns the total number of rows flushed across all buffers.
        """
        total = (
            len(self._observations)
            + len(self._wifi_frames)
            + len(self._probe_requests)
            + len(self._bt_advertisements)
            + len(self._sdr_adsb)
            + len(self._sdr_tpms)
            + len(self._sdr_weather)
            + len(self._sdr_ism)
        )
        if total == 0:
            return 0
        count = total

        # Observations — list() copies avoid race with clear() below
        if self._observations:
            self._writer.execute_many(
                "INSERT INTO observations "
                "(timestamp, mac, source, rssi, channel, latitude, longitude, "
                "identity_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(self._observations),
            )

        # WiFi frames
        if self._wifi_frames:
            self._writer.execute_many(
                "INSERT INTO wifi_frames "
                "(timestamp, src_mac, dst_mac, bssid, ssid, channel, rssi, "
                "frame_type, frame_subtype, sequence_num, identity_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(self._wifi_frames),
            )

        # Probe requests
        if self._probe_requests:
            self._writer.execute_many(
                "INSERT INTO probe_requests "
                "(timestamp, mac, ssid, rssi, channel, sequence_num, ie_bytes, "
                "ie_fingerprint_hash, identity_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(self._probe_requests),
            )

        # BT advertisements
        if self._bt_advertisements:
            self._writer.execute_many(
                "INSERT INTO bt_advertisements "
                "(timestamp, mac, device_name, rssi, tx_power, manufacturer_data_hex, "
                "service_uuids, device_class, is_classic, mfr_fingerprint_hash, "
                "identity_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(self._bt_advertisements),
            )

        # ADS-B aircraft messages (Stage 18b)
        if self._sdr_adsb:
            self._writer.execute_many(
                "INSERT INTO sdr_adsb "
                "(timestamp, icao_hex, callsign, altitude_ft, ground_speed_kt, "
                "track_deg, latitude, longitude, vertical_rate_fpm, squawk, "
                "rssi_dbfs, message_type, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(self._sdr_adsb),
            )

        # Stage D: 433 MHz capture — three target tables.
        if self._sdr_tpms:
            self._writer.execute_many(
                "INSERT INTO sdr_tpms "
                "(timestamp, sensor_id, protocol, pressure_kpa, temperature_c, "
                "battery_low, rssi, flags, identity_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(self._sdr_tpms),
            )

        if self._sdr_weather:
            self._writer.execute_many(
                "INSERT INTO sdr_weather "
                "(timestamp, station_id, protocol, temperature_c, humidity, "
                "wind_kph, rain_mm, battery_low, rssi, identity_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(self._sdr_weather),
            )

        if self._sdr_ism:
            self._writer.execute_many(
                "INSERT INTO sdr_ism "
                "(timestamp, device_id, protocol, category, rssi, identity_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                list(self._sdr_ism),
            )

        # Device upserts
        for mac, fields in self._device_upserts.items():
            self._writer.execute(
                "INSERT INTO devices (mac, first_seen, last_seen, vendor, device_name, "
                "device_type, is_ap, identity_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mac) DO UPDATE SET "
                "last_seen = MAX(excluded.last_seen, devices.last_seen), "
                "vendor = COALESCE(excluded.vendor, devices.vendor), "
                "device_name = COALESCE(excluded.device_name, devices.device_name), "
                "is_ap = MAX(excluded.is_ap, devices.is_ap), "
                "identity_id = COALESCE(excluded.identity_id, devices.identity_id)",
                (
                    mac,
                    fields["first_seen"],
                    fields["last_seen"],
                    fields["vendor"],
                    fields["device_name"],
                    fields["device_type"],
                    fields["is_ap"],
                    fields.get("identity_id"),
                ),
            )

        self._total_flushed += count
        self._last_flush = time.monotonic()

        # Clear buffers
        self._observations.clear()
        self._wifi_frames.clear()
        self._probe_requests.clear()
        self._bt_advertisements.clear()
        self._sdr_adsb.clear()
        self._sdr_tpms.clear()
        self._sdr_weather.clear()
        self._sdr_ism.clear()
        self._device_upserts.clear()

        return count

    @property
    def buffered_count(self) -> int:
        """Number of observations currently buffered."""
        return len(self._observations)

    @property
    def total_flushed(self) -> int:
        """Total observations flushed since start."""
        return self._total_flushed


def enrich_event(
    event: dict[str, Any],
    cfg: Any,
    identity_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Enrich a raw capture event with derived fields.

    Adds: vendor (OUI), latitude/longitude (GPS or static fallback),
    device_type, is_ap flag, identity_id (from YAML dossier).
    """
    mac = event.get("mac", "")

    # OUI vendor lookup
    if "vendor" not in event or event["vendor"] is None:
        event["vendor"] = lookup_vendor(mac)

    # Stage 15: Identity tagging from YAML dossier.
    # Skip if no map loaded (graceful when identities/ doesn't exist).
    if identity_map:
        sensor_id = (
            event.get("sensor_id")
            or event.get("device_id")
            or event.get("station_id")
        )
        identifier = mac or sensor_id
        if identifier:
            identity = lookup_identity(str(identifier), identity_map)
            if identity:
                event["identity_id"] = identity

    # GPS location: use static fallback from config
    if "latitude" not in event or event["latitude"] is None:
        event["latitude"] = cfg.static_location.latitude
        event["longitude"] = cfg.static_location.longitude

    # Device type inference
    source = event.get("source", "unknown")
    if source == "wifi":
        event["device_type"] = "wifi"
        # Detect APs from beacons and probe responses
        frame_subtype = event.get("frame_subtype")
        if frame_subtype in _AP_SUBTYPES:
            src = event.get("src_mac", mac)
            bssid = event.get("bssid")
            if src and bssid and src == bssid:
                event["is_ap"] = True
    elif source == "bt":
        if event.get("is_classic"):
            event["device_type"] = "bt_classic"
        else:
            event["device_type"] = "ble"

    return event


class IngestDaemon:
    """Main ingestion daemon — ties together bus, enrichment, dedup, batching.

    Lifecycle:
        1. Apply DB schema (idempotent)
        2. Start DatabaseWriter
        3. Start BusServer
        4. Process events until SIGTERM/SIGINT
        5. Flush remaining batch, stop writer, stop server
    """

    def __init__(self, config_path: str | None = None) -> None:
        if config_path:
            load_config(config_path)
        self._cfg = get_config()
        self._running = True

        self._dedup = Deduplicator(window_s=self._cfg.ingest.dedup_window_s)
        self._writer: DatabaseWriter | None = None
        self._batcher: EventBatcher | None = None
        self._server: BusServer | None = None

        self._event_count = 0
        self._dedup_count = 0
        self._error_count = 0

        # Stage 15: Identity map populated by _reload_identities at startup
        # and on SIGHUP. Empty until run() loads it.
        self._identity_map: dict[str, str] = {}

    def _reload_identities(self) -> None:
        """Reload identity map from YAML dossier. Called at startup and on SIGHUP."""
        identities_dir = self._cfg.resolved_db_path.parent / "identities"
        try:
            self._identity_map = load_identity_map(identities_dir)
            logger.info(
                "Reloaded %d identifiers from identities dir: %s",
                len(self._identity_map), identities_dir,
            )
        except Exception as exc:
            logger.error("Failed to reload identities: %s", exc)
            # Keep existing map intact on reload failure

    def _on_sighup(self) -> None:
        """Combined SIGHUP handler: reload detection thresholds + identities."""
        try:
            reload_thresholds()
        except Exception:
            logger.exception("Failed to reload detection thresholds")
        self._reload_identities()

    async def run(self) -> None:
        """Main entry point."""
        install_sighup_handler()

        logger.info("Ingestion daemon starting")

        # Apply schema
        db_path = self._cfg.resolved_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        apply_schema(db_path)
        logger.info("Database schema applied: %s", db_path)

        # Start writer
        self._writer = DatabaseWriter(db_path)
        self._writer.start()

        # Create batcher
        self._batcher = EventBatcher(
            writer=self._writer,
            batch_interval_s=self._cfg.ingest.batch_interval_s,
            batch_max_events=self._cfg.ingest.batch_max_events,
        )

        # Start bus server
        self._server = BusServer()
        self._server.add_handler(self._handle_event)
        await self._server.start()

        # Stage 15: Load identity dossier once at startup. SIGHUP handler
        # below triggers reload at runtime without daemon restart.
        self._reload_identities()

        # Install signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown, sig)
        # Stage 15: SIGHUP triggers identity reload (and detection-threshold
        # reload, so the existing config hot-reload behavior is preserved).
        loop.add_signal_handler(signal.SIGHUP, self._on_sighup)

        logger.info("Ingestion daemon running — awaiting events")

        # Main loop: periodic flush check
        try:
            while self._running:
                await asyncio.sleep(0.5)
                if self._batcher.should_flush():
                    flushed = self._batcher.flush()
                    if flushed > 0:
                        logger.debug(
                            "Flushed %d events (total: %d, deduped: %d)",
                            flushed, self._event_count, self._dedup_count,
                        )
        except asyncio.CancelledError:
            pass

        # Shutdown
        await self._shutdown_async()

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a single event from the bus (called synchronously by BusServer)."""
        try:
            # Validate — source and timestamp are required for every event.
            # MAC is only required for the wifi/bt pipeline; ADS-B events
            # carry icao_hex instead and skip the mac-keyed path entirely.
            source = event.get("source")
            timestamp = event.get("timestamp")
            if not source or not timestamp:
                self._error_count += 1
                return

            if source in NON_MAC_SOURCES:
                # ADS-B / sdr_433: skip MAC validation, enrich_event
                # (OUI vendor lookup is mac-keyed), and dedup (also
                # mac-keyed). EventBatcher.add() routes by source —
                # and for sdr_433, by the _target_table control field.
                # Stage 15: still apply identity tagging for sdr_433
                # (sensor_id / station_id / device_id) since enrich_event
                # is bypassed entirely on this path.
                if self._identity_map:
                    sensor_id = (
                        event.get("sensor_id")
                        or event.get("device_id")
                        or event.get("station_id")
                    )
                    if sensor_id:
                        identity = lookup_identity(
                            str(sensor_id), self._identity_map
                        )
                        if identity:
                            event["identity_id"] = identity
                assert self._batcher is not None
                self._batcher.add(event)
                self._event_count += 1
                return

            mac = event.get("mac")
            if not mac:
                self._error_count += 1
                return

            # Enrich (Stage 15: pass identity_map for tagging)
            event = enrich_event(event, self._cfg, self._identity_map)

            # Dedup
            channel = event.get("channel")
            if self._dedup.is_duplicate(mac, channel):
                self._dedup_count += 1
                return

            # Buffer
            assert self._batcher is not None
            self._batcher.add(event)
            self._event_count += 1

        except Exception:
            self._error_count += 1
            logger.exception("Error processing event")

    def _shutdown(self, sig: signal.Signals) -> None:
        """Signal handler for graceful shutdown."""
        logger.info("Received %s, shutting down", sig.name)
        self._running = False

    async def _shutdown_async(self) -> None:
        """Async shutdown — flush and close everything."""
        logger.info("Flushing remaining events...")

        if self._batcher is not None:
            flushed = self._batcher.flush()
            logger.info("Final flush: %d events", flushed)

        if self._server is not None:
            await self._server.stop()

        if self._writer is not None:
            self._writer.stop()

        logger.info(
            "Ingestion daemon stopped. "
            "Events: %d processed, %d deduped, %d errors, %d total flushed",
            self._event_count,
            self._dedup_count,
            self._error_count,
            self._batcher.total_flushed if self._batcher else 0,
        )


async def main(args: Any) -> None:
    """Async entry point."""
    cfg = load_config(args.config)
    from sentinel.common.logging import setup_logging
    setup_logging("ingest")

    from sentinel.config import validate_config
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        sys.exit(1)

    daemon = IngestDaemon()
    await daemon.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel ingestion daemon")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config.yaml")
    parsed = parser.parse_args()

    asyncio.run(main(parsed))
