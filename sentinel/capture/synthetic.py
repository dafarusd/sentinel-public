"""Synthetic event generator for development and testing.

Emits realistic-looking WiFi and BT events to the ingest bus at configurable
rates. Allows developing and testing ingest/profiler/detector on the
Framework 16 without needing a Pi + Alfa.

Usage:
    python -m sentinel.capture.synthetic
    python -m sentinel.capture.synthetic --rate 100 --duration 60
    python -m sentinel.capture.synthetic --scenario office
"""

from __future__ import annotations

import argparse
import asyncio
import random
import signal
import sys
from datetime import datetime, timezone
from typing import Any

import logging
from sentinel.config import load_config
from sentinel.ingest.bus import BusClient

logger = logging.getLogger("sentinel.synthetic")

# ---------------------------------------------------------------------------
# Device pools — realistic test data
# ---------------------------------------------------------------------------

# Known "regular" devices (appear predictably)
REGULAR_DEVICES: list[dict[str, Any]] = [
    {"mac": "a4:83:e7:11:22:33", "name": "iPhone-Alice", "type": "wifi",
     "ssids": ["HomeNet", "CoffeeShop-5G"], "rssi_base": -45},
    {"mac": "b0:be:76:44:55:66", "name": "Galaxy-Bob", "type": "wifi",
     "ssids": ["HomeNet", "Office-Corp"], "rssi_base": -52},
    {"mac": "dc:a6:32:77:88:99", "name": "Pi-Garage", "type": "wifi",
     "ssids": ["HomeNet"], "rssi_base": -35},
    {"mac": "f0:18:98:aa:bb:cc", "name": "MacBook-Carol", "type": "wifi",
     "ssids": ["Office-Corp", "HomeNet", "Starbucks"], "rssi_base": -48},
    {"mac": "00:1a:7d:dd:ee:ff", "name": "Laptop-Dave", "type": "wifi",
     "ssids": ["Office-Corp"], "rssi_base": -55},
]

# BLE devices (always broadcasting)
BLE_DEVICES: list[dict[str, Any]] = [
    {"mac": "c8:28:32:11:aa:01", "name": "Fitbit-Charge5", "rssi_base": -60,
     "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"]},
    {"mac": "e4:5f:01:22:bb:02", "name": "AirPods-Pro", "rssi_base": -50,
     "service_uuids": ["0000110b-0000-1000-8000-00805f9b34fb"]},
    {"mac": "d0:03:4b:33:cc:03", "name": None, "rssi_base": -70,
     "service_uuids": []},
]

# Randomized MACs that share SSID sets (for probe cluster testing)
RANDOMIZED_PROBE_SETS: list[list[dict[str, Any]]] = [
    # Cluster 1: likely same device, rotating MACs, probing for same networks
    [
        {"mac": "da:a1:19:00:01:01", "ssids": ["HomeNet", "CoffeeShop-5G", "Airport-Free"]},
        {"mac": "da:a1:19:00:01:02", "ssids": ["HomeNet", "CoffeeShop-5G"]},
        {"mac": "da:a1:19:00:01:03", "ssids": ["HomeNet", "CoffeeShop-5G", "Airport-Free"]},
    ],
    # Cluster 2: another device
    [
        {"mac": "fa:b2:3c:00:02:01", "ssids": ["Office-Corp", "Hotel-Lobby"]},
        {"mac": "fa:b2:3c:00:02:02", "ssids": ["Office-Corp", "Hotel-Lobby", "Gym-5G"]},
    ],
]

AP_LIST = [
    {"bssid": "00:1f:f3:aa:00:01", "ssid": "HomeNet", "channel": 6},
    {"bssid": "00:1f:f3:bb:00:02", "ssid": "Office-Corp", "channel": 36},
    {"bssid": "00:1f:f3:cc:00:03", "ssid": "CoffeeShop-5G", "channel": 149},
]

# Frame types
FRAME_MGMT = 0
SUBTYPE_PROBE_REQ = 4
SUBTYPE_PROBE_RESP = 5
SUBTYPE_BEACON = 8
FRAME_DATA = 2
SUBTYPE_DATA = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jitter_rssi(base: int, spread: int = 8) -> int:
    return base + random.randint(-spread, spread)


def _make_wifi_event(device: dict[str, Any]) -> dict[str, Any]:
    """Generate a WiFi observation event."""
    ap = random.choice(AP_LIST)
    return {
        "timestamp": _now(),
        "mac": device["mac"],
        "src_mac": device["mac"],
        "dst_mac": "ff:ff:ff:ff:ff:ff",
        "bssid": ap["bssid"],
        "ssid": ap["ssid"],
        "channel": ap["channel"],
        "rssi": _jitter_rssi(device["rssi_base"]),
        "frame_type": FRAME_DATA,
        "frame_subtype": SUBTYPE_DATA,
        "sequence_num": random.randint(0, 4095),
        "source": "wifi",
    }


def _make_probe_event(mac: str, ssid: str | None, rssi_base: int = -55) -> dict[str, Any]:
    """Generate a probe request event."""
    return {
        "timestamp": _now(),
        "mac": mac,
        "src_mac": mac,
        "dst_mac": "ff:ff:ff:ff:ff:ff",
        "bssid": "ff:ff:ff:ff:ff:ff",
        "ssid": ssid,
        "channel": random.choice([1, 6, 11, 36, 149]),
        "rssi": _jitter_rssi(rssi_base),
        "frame_type": FRAME_MGMT,
        "frame_subtype": SUBTYPE_PROBE_REQ,
        "sequence_num": random.randint(0, 4095),
        "source": "wifi",
    }


def _make_ble_event(device: dict[str, Any]) -> dict[str, Any]:
    """Generate a BLE advertisement event."""
    return {
        "timestamp": _now(),
        "mac": device["mac"],
        "device_name": device["name"],
        "rssi": _jitter_rssi(device["rssi_base"]),
        "tx_power": random.choice([-12, -8, -4, 0, 4]),
        "manufacturer_data_hex": f"{random.randint(0, 0xFFFF):04x}",
        "service_uuids": device.get("service_uuids", []),
        "device_class": None,
        "is_classic": False,
        "source": "bt",
    }


def _make_anomalous_event() -> dict[str, Any]:
    """Generate a random anomalous event — new device or unusual time."""
    new_mac = "00:de:ad:{:02x}:{:02x}:{:02x}".format(
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
    )
    return {
        "timestamp": _now(),
        "mac": new_mac,
        "src_mac": new_mac,
        "dst_mac": "ff:ff:ff:ff:ff:ff",
        "bssid": "ff:ff:ff:ff:ff:ff",
        "ssid": None,
        "channel": random.choice([1, 6, 11]),
        "rssi": _jitter_rssi(-70, spread=15),
        "frame_type": FRAME_MGMT,
        "frame_subtype": SUBTYPE_PROBE_REQ,
        "sequence_num": random.randint(0, 4095),
        "source": "wifi",
    }


async def generate_events(
    client: BusClient,
    rate: float = 10.0,
    duration: float | None = None,
    anomaly_pct: float = 5.0,
) -> None:
    """Generate and send synthetic events at the specified rate.

    Args:
        client: Connected BusClient.
        rate: Events per second.
        duration: Run for this many seconds, or None for indefinite.
        anomaly_pct: Percentage of events that are anomalous (new devices).
    """
    interval = 1.0 / rate
    count = 0
    running = True

    def stop_handler(sig: signal.Signals) -> None:
        nonlocal running
        running = False

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_handler, sig)

    start = asyncio.get_event_loop().time()

    while running:
        if duration and (asyncio.get_event_loop().time() - start) > duration:
            break

        # Pick event type
        roll = random.random() * 100
        if roll < anomaly_pct:
            event = _make_anomalous_event()
        elif roll < 30:
            # Probe request from regular device
            dev = random.choice(REGULAR_DEVICES)
            ssid = random.choice(dev["ssids"]) if random.random() > 0.2 else None
            event = _make_probe_event(dev["mac"], ssid, dev["rssi_base"])
        elif roll < 45:
            # Probe from randomized MAC cluster
            cluster = random.choice(RANDOMIZED_PROBE_SETS)
            member = random.choice(cluster)
            ssid = random.choice(member["ssids"])
            event = _make_probe_event(member["mac"], ssid)
        elif roll < 70:
            # Regular WiFi data frame
            dev = random.choice(REGULAR_DEVICES)
            event = _make_wifi_event(dev)
        else:
            # BLE advertisement
            dev = random.choice(BLE_DEVICES)
            event = _make_ble_event(dev)

        await client.send(event)
        count += 1

        if count % 100 == 0:
            logger.info("Sent %d events (%.1f/s)", count, rate)

        await asyncio.sleep(interval)

    logger.info("Synthetic generator finished: %d events sent", count)


async def main(args: argparse.Namespace) -> None:
    """Main async entry point."""
    load_config(args.config)

    from sentinel.common.logging import setup_logging
    setup_logging("synthetic")

    client = BusClient()
    await client.connect()

    try:
        await generate_events(
            client,
            rate=args.rate,
            duration=args.duration,
            anomaly_pct=args.anomaly_pct,
        )
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sentinel synthetic event generator for dev/test"
    )
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--rate", "-r", type=float, default=10.0,
                        help="Events per second (default: 10)")
    parser.add_argument("--duration", "-d", type=float, default=None,
                        help="Run for N seconds (default: indefinite)")
    parser.add_argument("--anomaly-pct", type=float, default=5.0,
                        help="Percentage of anomalous events (default: 5)")
    parsed = parser.parse_args()

    asyncio.run(main(parsed))
