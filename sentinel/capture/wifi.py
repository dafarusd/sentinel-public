"""WiFi capture daemon for Sentinel.

Uses scapy in monitor mode to capture 802.11 frames from the Alfa AWUS036ACH
(or any monitor-mode-capable interface). Parses Dot11 and RadioTap headers,
extracts all specified fields, and emits normalized events to the ingest bus.

Includes a channel hopper thread that cycles through configured 2.4 GHz and
5 GHz channels with configurable dwell time.

Requires: monitor-mode capable interface, root or CAP_NET_RAW.

Usage:
    python -m sentinel.capture.wifi
    python -m sentinel.capture.wifi --config /path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sentinel.capture.base import BaseCaptureD
from sentinel.config import get_config
from sentinel.profiler.fingerprint import ie_fingerprint_hash

logger = logging.getLogger("sentinel.wifi")

# 802.11 frame type/subtype constants
FRAME_TYPE_MGMT = 0
FRAME_TYPE_CTRL = 1
FRAME_TYPE_DATA = 2

SUBTYPE_ASSOC_REQ = 0
SUBTYPE_ASSOC_RESP = 1
SUBTYPE_PROBE_REQ = 4
SUBTYPE_PROBE_RESP = 5
SUBTYPE_BEACON = 8
SUBTYPE_DISASSOC = 10
SUBTYPE_AUTH = 11
SUBTYPE_DEAUTH = 12


def _run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a shell command, logging it."""
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


class ChannelHopper:
    """Background thread that cycles the WiFi interface through channels.

    Hops through all configured 2.4 GHz and 5 GHz channels using `iw`.
    """

    def __init__(self, interface: str, channels: list[int], dwell_ms: int) -> None:
        self._interface = interface
        self._channels = channels
        self._dwell_s = dwell_ms / 1000.0
        self._current_channel: int = channels[0] if channels else 1
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def current_channel(self) -> int:
        """The channel the interface is currently set to."""
        with self._lock:
            return self._current_channel

    def start(self) -> None:
        """Start the channel hopping thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._hop_loop, name="channel-hopper", daemon=True
        )
        self._thread.start()
        logger.info(
            "Channel hopper started: %d channels, %dms dwell",
            len(self._channels), int(self._dwell_s * 1000),
        )

    def stop(self) -> None:
        """Stop the channel hopping thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Channel hopper stopped on channel %d", self._current_channel)

    def _hop_loop(self) -> None:
        """Main hop loop — cycles through channels indefinitely."""
        idx = 0
        while self._running:
            channel = self._channels[idx % len(self._channels)]
            try:
                result = _run_cmd(
                    ["iw", "dev", self._interface, "set", "channel", str(channel)],
                    check=False,
                )
                if result.returncode == 0:
                    with self._lock:
                        self._current_channel = channel
                else:
                    # Some 5 GHz channels may fail due to DFS/regulatory
                    logger.debug(
                        "Failed to set channel %d: %s",
                        channel, result.stderr.strip(),
                    )
            except Exception:
                logger.exception("Channel hop error on channel %d", channel)

            idx += 1
            time.sleep(self._dwell_s)


class MonitorMode:
    """Manage monitor mode on a WiFi interface.

    Tries `iw` first (lightweight), falls back to `airmon-ng` if needed.
    Tracks the monitor interface name (which may differ from the original).
    """

    def __init__(self, interface: str) -> None:
        self._original = interface
        self._mon_interface: str | None = None
        self._used_airmon = False

    @property
    def interface(self) -> str:
        """The active monitor-mode interface name."""
        return self._mon_interface or self._original

    def enable(self) -> str:
        """Enable monitor mode. Returns the monitor interface name."""
        # Check if already in monitor mode
        if self._is_monitor_mode(self._original):
            logger.info("Interface %s already in monitor mode", self._original)
            self._mon_interface = self._original
            return self._original

        # Try iw approach first
        try:
            return self._enable_iw()
        except Exception as exc:
            logger.warning("iw monitor mode failed: %s — trying airmon-ng", exc)

        # Fall back to airmon-ng
        return self._enable_airmon()

    def disable(self) -> None:
        """Disable monitor mode and restore the interface."""
        if self._mon_interface is None:
            return

        try:
            if self._used_airmon:
                _run_cmd(["airmon-ng", "stop", self._mon_interface], check=False)
                logger.info("Disabled monitor mode via airmon-ng on %s", self._mon_interface)
            else:
                _run_cmd(["ip", "link", "set", self._mon_interface, "down"], check=False)
                _run_cmd(
                    ["iw", "dev", self._mon_interface, "set", "type", "managed"],
                    check=False,
                )
                _run_cmd(["ip", "link", "set", self._mon_interface, "up"], check=False)
                logger.info("Disabled monitor mode via iw on %s", self._mon_interface)
        except Exception:
            logger.exception("Error disabling monitor mode")
        finally:
            self._mon_interface = None

    def _is_monitor_mode(self, iface: str) -> bool:
        """Check if an interface is already in monitor mode."""
        result = _run_cmd(["iw", "dev", iface, "info"], check=False)
        return "type monitor" in result.stdout

    def _enable_iw(self) -> str:
        """Enable monitor mode using iw commands.

        Stage 18: do NOT call `airmon-ng check kill` here. That command
        kills wpa_supplicant, NetworkManager, dhclient, and avahi-daemon
        systemwide — not just for the target interface. On Pi 5 with
        wlan0 used for home WiFi connectivity, this breaks the home
        network on every sentinel-wifi startup. The iw command alone
        successfully puts the Alfa AWUS036ACH (RTL8812AU) into monitor
        mode without disturbing other interfaces.

        If iw fails for some reason, _enable_airmon (the fallback path)
        will still run airmon-ng start — which DOES need check kill,
        but that path is only reached when the lightweight approach has
        already failed.
        """
        _run_cmd(["ip", "link", "set", self._original, "down"])
        _run_cmd(["iw", "dev", self._original, "set", "type", "monitor"])
        _run_cmd(["ip", "link", "set", self._original, "up"])

        if not self._is_monitor_mode(self._original):
            raise RuntimeError(f"iw failed to set {self._original} to monitor mode")

        self._mon_interface = self._original
        self._used_airmon = False
        logger.info("Monitor mode enabled via iw on %s", self._original)
        return self._original

    def _enable_airmon(self) -> str:
        """Enable monitor mode using airmon-ng."""
        _run_cmd(["airmon-ng", "check", "kill"], check=False)
        result = _run_cmd(["airmon-ng", "start", self._original])

        # airmon-ng may rename the interface (e.g. wlan1 -> wlan1mon)
        mon_name = self._original + "mon"

        # Parse output to find actual monitor interface name
        for line in result.stdout.splitlines():
            if "monitor mode" in line.lower() and "enabled" in line.lower():
                # Try to extract interface name from parenthetical
                # e.g. "(monitor mode vif enabled for [phy0]wlan1mon on [phy0]wlan1)"
                parts = line.split()
                for part in parts:
                    if "mon" in part and part.strip("[]()") != "":
                        candidate = part.strip("[]()").split("]")[-1]
                        if candidate:
                            mon_name = candidate
                            break

        # Verify the monitor interface exists
        check = _run_cmd(["iw", "dev", mon_name, "info"], check=False)
        if check.returncode != 0:
            # Try the original name in case airmon-ng didn't rename
            if self._is_monitor_mode(self._original):
                mon_name = self._original

        self._mon_interface = mon_name
        self._used_airmon = True
        logger.info("Monitor mode enabled via airmon-ng: %s -> %s", self._original, mon_name)
        return mon_name


def _parse_packet(packet: Any, hopper: ChannelHopper | None) -> dict[str, Any] | None:
    """Parse a scapy packet into a Sentinel event dict.

    Returns None if the packet can't be parsed (non-802.11, malformed, etc.).
    """
    # Import scapy layers here to avoid import cost at module level
    from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Elt, Dot11ProbeReq, Dot11ProbeResp

    if not packet.haslayer(Dot11):
        return None

    dot11 = packet.getlayer(Dot11)

    # Extract frame type and subtype
    frame_type = dot11.type
    frame_subtype = dot11.subtype

    # Extract addresses
    # addr1 = destination, addr2 = source, addr3 = BSSID (for most mgmt/data)
    _ZERO_MAC = "00:00:00:00:00:00"
    src_mac = _normalize_mac(dot11.addr2) if dot11.addr2 else None
    dst_mac = _normalize_mac(dot11.addr1) if dot11.addr1 else None
    bssid = _normalize_mac(dot11.addr3) if dot11.addr3 else None

    if src_mac is None or src_mac == _ZERO_MAC:
        return None  # Can't do anything without a real source MAC

    # Extract RSSI from RadioTap header
    rssi = _extract_rssi(packet)

    # Extract sequence number (12-bit field from SC field)
    sequence_num = None
    if hasattr(dot11, "SC") and dot11.SC is not None:
        sequence_num = dot11.SC >> 4  # Upper 12 bits are sequence number

    # Determine channel — prefer RadioTap, fall back to hopper
    channel = _extract_channel(packet)
    if channel is None and hopper is not None:
        channel = hopper.current_channel

    # Extract SSID for management frames
    ssid = None
    ie_bytes_raw = None
    if frame_type == FRAME_TYPE_MGMT and frame_subtype in (
        SUBTYPE_PROBE_REQ, SUBTYPE_PROBE_RESP, SUBTYPE_BEACON
    ):
        ssid, ie_bytes_raw = _extract_ssid_and_ies(packet)

    timestamp = datetime.now(timezone.utc).isoformat()

    event: dict[str, Any] = {
        "timestamp": timestamp,
        "mac": src_mac,
        "src_mac": src_mac,
        "dst_mac": dst_mac,
        "bssid": bssid,
        "ssid": ssid,
        "channel": channel,
        "rssi": rssi,
        "frame_type": frame_type,
        "frame_subtype": frame_subtype,
        "sequence_num": sequence_num,
    }

    # Include raw IE bytes for probe requests (Level C prep)
    if frame_subtype == SUBTYPE_PROBE_REQ and ie_bytes_raw is not None:
        # Hex-encode for JSON transport
        event["ie_bytes_hex"] = ie_bytes_raw.hex()
        # Stage 14a: canonical IE fingerprint. None when the probe has no
        # fingerprintable structure — stored as NULL downstream.
        fp = ie_fingerprint_hash(ie_bytes_raw)
        if fp is not None:
            event["ie_fingerprint_hash"] = fp

    return event


def _normalize_mac(mac: str | None) -> str | None:
    """Normalize a MAC address to lowercase colon-separated format."""
    if mac is None:
        return None
    return mac.lower().strip()


def _extract_rssi(packet: Any) -> int | None:
    """Extract RSSI (dBm) from a RadioTap header."""
    try:
        if hasattr(packet, "dBm_AntSignal"):
            return int(packet.dBm_AntSignal)
    except (TypeError, ValueError):
        pass

    # Some drivers use different field names
    try:
        if hasattr(packet, "notdecoded"):
            # Manual RadioTap parsing fallback for some drivers
            # The Alfa AWUS036ACH rtl8812au driver usually provides dBm_AntSignal
            pass
    except Exception:
        pass

    return None


def _extract_channel(packet: Any) -> int | None:
    """Extract channel number from RadioTap header."""
    try:
        if hasattr(packet, "ChannelFrequency") and packet.ChannelFrequency:
            freq = int(packet.ChannelFrequency)
            return _freq_to_channel(freq)
    except (TypeError, ValueError):
        pass
    return None


def _freq_to_channel(freq_mhz: int) -> int | None:
    """Convert a WiFi frequency in MHz to a channel number."""
    if 2412 <= freq_mhz <= 2484:
        if freq_mhz == 2484:
            return 14
        return (freq_mhz - 2407) // 5
    elif 5170 <= freq_mhz <= 5835:
        return (freq_mhz - 5000) // 5
    return None


def _extract_ssid_and_ies(packet: Any) -> tuple[str | None, bytes | None]:
    """Extract SSID and raw Information Elements from a management frame."""
    from scapy.layers.dot11 import Dot11Elt

    ssid = None
    ie_bytes_parts: list[bytes] = []

    elt = packet.getlayer(Dot11Elt)
    while elt:
        # Collect raw IE bytes for Level C prep
        try:
            ie_bytes_parts.append(bytes([elt.ID, elt.len]) + elt.info)
        except (AttributeError, TypeError):
            pass

        # ID 0 = SSID
        if elt.ID == 0:
            try:
                raw_ssid = elt.info.decode("utf-8", errors="replace").strip("\x00")
                if raw_ssid:
                    ssid = raw_ssid
            except (AttributeError, UnicodeDecodeError):
                pass

        elt = elt.payload.getlayer(Dot11Elt) if elt.payload else None

    ie_bytes = b"".join(ie_bytes_parts) if ie_bytes_parts else None
    return ssid, ie_bytes


class WifiCaptureD(BaseCaptureD):
    """WiFi capture daemon using scapy in monitor mode."""

    @property
    def name(self) -> str:
        return "wifi"

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__(config_path)
        self._wifi_cfg = self._cfg.wifi
        self._monitor: MonitorMode | None = None
        self._hopper: ChannelHopper | None = None
        self._packet_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)

    async def _setup(self) -> None:
        """Enable monitor mode and start channel hopper."""
        if not self._wifi_cfg.enabled:
            self._logger.warning("WiFi capture disabled in config")
            raise SystemExit(0)

        self._monitor = MonitorMode(self._wifi_cfg.interface)
        mon_iface = self._monitor.enable()
        self._logger.info("Monitor mode active on %s", mon_iface)

        if self._wifi_cfg.hop_enabled:
            all_channels = (
                self._wifi_cfg.channels_24ghz + self._wifi_cfg.channels_5ghz
            )
            self._hopper = ChannelHopper(
                interface=mon_iface,
                channels=all_channels,
                dwell_ms=self._wifi_cfg.channel_dwell_ms,
            )
            self._hopper.start()

    async def _capture(self) -> AsyncIterator[dict[str, Any]]:
        """Sniff packets with scapy and yield parsed events."""
        from scapy.all import AsyncSniffer

        assert self._monitor is not None
        iface = self._monitor.interface

        # scapy's AsyncSniffer runs in a background thread.
        # We bridge it to our async loop via a queue.
        def _packet_callback(packet: Any) -> None:
            event = _parse_packet(packet, self._hopper)
            if event is not None:
                try:
                    self._packet_queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # Drop if queue backs up — we prefer not blocking scapy

        sniffer = AsyncSniffer(
            iface=iface,
            prn=_packet_callback,
            store=False,  # Don't accumulate packets in memory
        )
        sniffer.start()
        self._logger.info("Scapy sniffer started on %s", iface)

        try:
            while self._running:
                try:
                    event = await asyncio.wait_for(
                        self._packet_queue.get(), timeout=1.0
                    )
                    yield event
                except asyncio.TimeoutError:
                    continue  # Check self._running
        finally:
            sniffer.stop()
            self._logger.info("Scapy sniffer stopped")

    async def _teardown(self) -> None:
        """Stop channel hopper and disable monitor mode."""
        if self._hopper is not None:
            self._hopper.stop()

        if self._monitor is not None:
            self._monitor.disable()


if __name__ == "__main__":
    BaseCaptureD.entrypoint(WifiCaptureD)
