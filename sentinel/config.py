"""Configuration loader for Sentinel.

Reads config.yaml once at startup. Detection thresholds are hot-reloadable
via SIGHUP — call reload_thresholds() or register the signal handler.
"""

from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class LocationConfig:
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    label: str = "default"


@dataclass(frozen=True)
class WifiConfig:
    enabled: bool = True
    interface: str = "wlan1"
    channels_24ghz: list[int] = field(default_factory=lambda: list(range(1, 12)))
    channels_5ghz: list[int] = field(default_factory=lambda: [
        36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
        116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165,
    ])
    channel_dwell_ms: int = 250
    hop_enabled: bool = True


@dataclass(frozen=True)
class BluetoothConfig:
    enabled: bool = True
    adapter: str = "hci0"
    ble_scan_duration_s: int = 10
    classic_inquiry_duration_s: int = 8
    classic_inquiry_interval_s: int = 60


@dataclass(frozen=True)
class SdrConfig:
    enabled: bool = False
    device_index: int = 0
    sample_rate: int = 2_400_000
    center_freq: int = 433_920_000
    gain: int = 40
    # Stage 18b: ADS-B subsystem. Defaults keep the daemon dormant on
    # hosts without an SDR so that loading an old config still works.
    adsb_enabled: bool = False
    adsb_readsb_host: str = "127.0.0.1"
    adsb_readsb_port: int = 30003
    adsb_reconnect_backoff_s: int = 2
    # Stage D: 433 MHz rtl_433 subsystem. Default off; mutually exclusive
    # with adsb_enabled in practice (single SDR), enforced operationally.
    rtl433_enabled: bool = False


@dataclass(frozen=True)
class GpsConfig:
    enabled: bool = False
    serial_port: str = "/dev/ttyS0"
    baud_rate: int = 9600


@dataclass(frozen=True)
class LoraConfig:
    enabled: bool = False
    serial_port: str = "/dev/ttyS0"
    frequency: int = 915_000_000


@dataclass(frozen=True)
class IngestConfig:
    batch_interval_s: float = 2.0
    batch_max_events: int = 500
    dedup_window_s: float = 1.0


@dataclass(frozen=True)
class ProfilerConfig:
    interval_min: int = 15
    companion_window_s: int = 60
    min_observations: int = 10


@dataclass
class DetectionConfig:
    """Detection thresholds — mutable for hot-reload via SIGHUP."""

    temporal_stddev: float = 3.0
    location_stddev: float = 2.0
    behavioral_probe_rate_multiplier: float = 3.0
    absence_presence_pct: float = 95.0
    absence_hours: float = 4.0
    probe_cluster_jaccard: float = 0.6


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    max_bytes: int = 10_485_760
    backup_count: int = 5


@dataclass
class SentinelConfig:
    """Top-level configuration. Immutable except for detection thresholds."""

    install_dir: Path = Path("/home/user/sentinel")
    data_dir: Path = Path("data")
    db_path: Path = Path("data/sentinel.db")
    log_dir: Path = Path("data/logs")
    socket_path: Path = Path("data/sentinel.sock")
    oui_path: Path = Path("data/oui.txt")
    static_location: LocationConfig = field(default_factory=LocationConfig)
    learning_period_days: int = 7
    wifi: WifiConfig = field(default_factory=WifiConfig)
    bluetooth: BluetoothConfig = field(default_factory=BluetoothConfig)
    sdr: SdrConfig = field(default_factory=SdrConfig)
    gps: GpsConfig = field(default_factory=GpsConfig)
    lora: LoraConfig = field(default_factory=LoraConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    profiler: ProfilerConfig = field(default_factory=ProfilerConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def resolve_path(self, p: Path) -> Path:
        """Resolve a path relative to install_dir if not absolute."""
        if p.is_absolute():
            return p
        return self.install_dir / p

    @property
    def resolved_db_path(self) -> Path:
        return self.resolve_path(self.db_path)

    @property
    def resolved_log_dir(self) -> Path:
        return self.resolve_path(self.log_dir)

    @property
    def resolved_socket_path(self) -> Path:
        return self.resolve_path(self.socket_path)

    @property
    def resolved_oui_path(self) -> Path:
        return self.resolve_path(self.oui_path)

    @property
    def resolved_data_dir(self) -> Path:
        return self.resolve_path(self.data_dir)


# ---------------------------------------------------------------------------
# Module-level config singleton
# ---------------------------------------------------------------------------

_config: SentinelConfig | None = None
_config_path: Path | None = None
_lock = threading.Lock()


def _build_dataclass(cls: type, data: dict[str, Any]) -> Any:
    """Build a dataclass from a dict, ignoring unknown keys."""
    import dataclasses
    valid_keys = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid_keys}
    return cls(**filtered)


def _parse_config(raw: dict[str, Any]) -> SentinelConfig:
    """Parse a raw YAML dict into a SentinelConfig."""
    cfg = SentinelConfig()

    # Simple top-level scalars
    for key in ("install_dir", "data_dir", "db_path", "log_dir", "socket_path", "oui_path"):
        if key in raw:
            object.__setattr__(cfg, key, Path(raw[key]))
    if "learning_period_days" in raw:
        cfg.learning_period_days = int(raw["learning_period_days"])

    # Nested sections
    if "static_location" in raw:
        cfg.static_location = _build_dataclass(LocationConfig, raw["static_location"])
    if "wifi" in raw:
        cfg.wifi = _build_dataclass(WifiConfig, raw["wifi"])
    if "bluetooth" in raw:
        cfg.bluetooth = _build_dataclass(BluetoothConfig, raw["bluetooth"])
    if "sdr" in raw:
        cfg.sdr = _build_dataclass(SdrConfig, raw["sdr"])
    if "gps" in raw:
        cfg.gps = _build_dataclass(GpsConfig, raw["gps"])
    if "lora" in raw:
        cfg.lora = _build_dataclass(LoraConfig, raw["lora"])
    if "ingest" in raw:
        cfg.ingest = _build_dataclass(IngestConfig, raw["ingest"])
    if "profiler" in raw:
        cfg.profiler = _build_dataclass(ProfilerConfig, raw["profiler"])
    if "detection" in raw:
        cfg.detection = _build_dataclass(DetectionConfig, raw["detection"])
    if "logging" in raw:
        cfg.logging = _build_dataclass(LoggingConfig, raw["logging"])

    return cfg


def validate_config(cfg: SentinelConfig) -> list[str]:
    """Validate a config and return a list of errors (empty = valid).

    Checks for obvious misconfigurations that would cause runtime failures.
    """
    errors: list[str] = []

    # Detection thresholds must be positive
    det = cfg.detection
    if det.temporal_stddev <= 0:
        errors.append(f"detection.temporal_stddev must be > 0, got {det.temporal_stddev}")
    if det.location_stddev <= 0:
        errors.append(f"detection.location_stddev must be > 0, got {det.location_stddev}")
    if det.behavioral_probe_rate_multiplier <= 0:
        errors.append(f"detection.behavioral_probe_rate_multiplier must be > 0, got {det.behavioral_probe_rate_multiplier}")
    if not (0 < det.probe_cluster_jaccard <= 1.0):
        errors.append(f"detection.probe_cluster_jaccard must be in (0, 1], got {det.probe_cluster_jaccard}")
    if det.absence_hours <= 0:
        errors.append(f"detection.absence_hours must be > 0, got {det.absence_hours}")

    # Ingest thresholds
    if cfg.ingest.batch_interval_s <= 0:
        errors.append(f"ingest.batch_interval_s must be > 0, got {cfg.ingest.batch_interval_s}")
    if cfg.ingest.batch_max_events <= 0:
        errors.append(f"ingest.batch_max_events must be > 0, got {cfg.ingest.batch_max_events}")

    # WiFi channel lists
    if cfg.wifi.enabled:
        all_channels = cfg.wifi.channels_24ghz + cfg.wifi.channels_5ghz
        if not all_channels:
            errors.append("wifi.channels_24ghz and wifi.channels_5ghz are both empty")
        if cfg.wifi.channel_dwell_ms < 50:
            errors.append(f"wifi.channel_dwell_ms too low ({cfg.wifi.channel_dwell_ms}ms), minimum 50ms")

    # Learning period
    if cfg.learning_period_days < 0:
        errors.append(f"learning_period_days must be >= 0, got {cfg.learning_period_days}")

    # Logging level
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if cfg.logging.level.upper() not in valid_levels:
        errors.append(f"logging.level must be one of {valid_levels}, got {cfg.logging.level}")

    return errors


def load_config(path: str | Path | None = None) -> SentinelConfig:
    """Load configuration from a YAML file.

    If path is None, looks for SENTINEL_CONFIG env var, then falls back to
    config.yaml in the current directory.
    """
    global _config, _config_path

    if path is None:
        path = os.environ.get("SENTINEL_CONFIG", "config.yaml")
    _config_path = Path(path)

    with open(_config_path) as f:
        raw = yaml.safe_load(f) or {}

    _config = _parse_config(raw)
    return _config


def get_config() -> SentinelConfig:
    """Return the loaded config, or load from default path."""
    if _config is None:
        return load_config()
    return _config


def reload_thresholds() -> None:
    """Hot-reload detection thresholds from the config file.

    Only updates detection.* values — everything else requires a restart.
    """
    global _config
    if _config is None or _config_path is None:
        return

    with _lock:
        with open(_config_path) as f:
            raw = yaml.safe_load(f) or {}

        if "detection" in raw:
            new_det = _build_dataclass(DetectionConfig, raw["detection"])
            _config.detection = new_det


def install_sighup_handler() -> None:
    """Register SIGHUP to hot-reload detection thresholds."""
    def _handler(signum: int, frame: Any) -> None:
        reload_thresholds()

    signal.signal(signal.SIGHUP, _handler)
