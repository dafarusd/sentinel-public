"""Detection daemon for Sentinel.

Scores observations against device profiles and writes anomalies to the
alerts table. Runs as a live daemon polling for new observations.

Anomaly types:
    new_device          — unseen MAC (info during learning, low after)
    temporal            — device outside historical time-of-day envelope (3+ stddev)
    location            — RSSI significantly stronger than p95 (2+ stddev)
    behavioral          — probing for new SSIDs or probe rate 3x+ historical mean
    absence             — high-presence device missing for >4h
    correlation         — device appears without usual companion(s)
    probe_set_cluster   — randomized MACs grouped by overlapping SSID sets

Learning mode: first 7 days after install, only new_device logs (at info level).

Usage:
    python -m sentinel.detector.engine
    python -m sentinel.detector.engine --config /path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import signal
import sqlite3
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sentinel.config import get_config, install_sighup_handler, load_config
from sentinel.db.writer import DatabaseWriter, get_readonly_connection

logger = logging.getLogger("sentinel.detector")


# ---------------------------------------------------------------------------
# Alert creation helper
# ---------------------------------------------------------------------------

def _make_alert(
    alert_type: str,
    severity: str,
    mac: str | None,
    summary: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an alert dict ready for DB insertion."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alert_type": alert_type,
        "severity": severity,
        "mac": mac,
        "summary": summary,
        "details_json": json.dumps(details) if details else None,
    }


def _write_alert(writer: DatabaseWriter, alert: dict[str, Any]) -> None:
    """Write an alert to the database."""
    writer.execute(
        "INSERT INTO alerts (timestamp, alert_type, severity, mac, summary, details_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            alert["timestamp"],
            alert["alert_type"],
            alert["severity"],
            alert["mac"],
            alert["summary"],
            alert["details_json"],
        ),
    )


# ---------------------------------------------------------------------------
# Learning mode
# ---------------------------------------------------------------------------

def is_learning_mode(conn: sqlite3.Connection, period_days: int = 7) -> bool:
    """Check if the system is still in learning mode.

    Learning mode is active for the first `period_days` after install.
    """
    row = conn.execute(
        "SELECT value FROM sentinel_meta WHERE key = 'installed_at'"
    ).fetchone()
    if row is None:
        return True  # No install date = assume learning

    try:
        installed = datetime.fromisoformat(row["value"])
    except (ValueError, TypeError):
        # installed_at from schema.sql uses datetime('now') which is naive UTC
        try:
            installed = datetime.strptime(row["value"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            return True

    if installed.tzinfo is None:
        installed = installed.replace(tzinfo=timezone.utc)

    cutoff = installed + timedelta(days=period_days)
    return datetime.now(timezone.utc) < cutoff


# ---------------------------------------------------------------------------
# Individual anomaly detectors
# ---------------------------------------------------------------------------

def detect_new_device(
    conn: sqlite3.Connection,
    mac: str,
    learning: bool,
) -> dict[str, Any] | None:
    """Check if this MAC has never been seen before.

    Returns alert dict or None.
    """
    row = conn.execute("SELECT first_seen FROM devices WHERE mac = ?", (mac,)).fetchone()
    if row is None:
        severity = "info" if learning else "low"
        return _make_alert(
            "new_device",
            severity,
            mac,
            f"New device detected: {mac}",
            {"learning_mode": learning},
        )
    return None


def detect_temporal(
    conn: sqlite3.Connection,
    mac: str,
    timestamp: str,
    threshold_stddev: float = 3.0,
) -> dict[str, Any] | None:
    """Check if device is present outside its historical time-of-day envelope.

    Fires when the current hour's presence count is 3+ stddev below the mean
    of active hours (i.e. the device is rarely seen at this hour).
    """
    profile = conn.execute(
        "SELECT time_histogram FROM device_profiles WHERE mac = ?", (mac,)
    ).fetchone()
    if profile is None:
        return None

    histogram = json.loads(profile["time_histogram"])
    if not histogram or sum(histogram) == 0:
        return None

    try:
        ts = datetime.fromisoformat(timestamp)
        current_hour = ts.hour
    except (ValueError, TypeError):
        return None

    current_count = histogram[current_hour]
    total_obs = sum(histogram)

    # Count active hours (hours with any observations)
    active_hours = sum(1 for v in histogram if v > 0)
    if active_hours < 3:
        return None  # Not enough temporal diversity

    # Compute stats across active hours only for threshold comparison
    active_values = [v for v in histogram if v > 0]
    active_mean = sum(active_values) / len(active_values)
    active_var = sum((v - active_mean) ** 2 for v in active_values) / len(active_values)
    active_stddev = math.sqrt(active_var)

    # Anomaly: device seen at an hour where it's rarely/never observed.
    # Primary signal: current hour has zero or near-zero observations while
    # the device has a clear pattern of activity in other hours.
    is_anomalous = False
    if current_count == 0 and active_hours >= 3:
        # Device has never been seen at this hour — anomalous
        is_anomalous = True
    elif active_stddev > 0 and current_count > 0:
        # Device has been seen at this hour, but very rarely compared to
        # its active hours. Use z-score against active-hour distribution.
        z_score = (active_mean - current_count) / active_stddev
        is_anomalous = z_score >= threshold_stddev

    if is_anomalous:
        return _make_alert(
            "temporal",
            "medium",
            mac,
            f"Device {mac} active at unusual hour {current_hour:02d}:00 "
            f"(count={current_count}, active_mean={active_mean:.1f}, "
            f"active_hours={active_hours})",
            {
                "hour": current_hour,
                "hour_count": current_count,
                "active_mean": round(active_mean, 2),
                "active_stddev": round(active_stddev, 2),
                "active_hours": active_hours,
                "histogram": histogram,
            },
        )
    return None


def detect_location(
    conn: sqlite3.Connection,
    mac: str,
    rssi: int | None,
    threshold_stddev: float = 2.0,
) -> dict[str, Any] | None:
    """Check if RSSI is significantly stronger than historical p95.

    A much stronger signal than usual suggests the device is physically
    closer than it has historically been.
    """
    if rssi is None:
        return None

    profile = conn.execute(
        "SELECT rssi_mean, rssi_stddev, rssi_p95 FROM device_profiles WHERE mac = ?",
        (mac,),
    ).fetchone()
    if profile is None or profile["rssi_p95"] is None or profile["rssi_stddev"] is None:
        return None

    p95 = profile["rssi_p95"]
    stddev = profile["rssi_stddev"]
    if stddev == 0:
        return None

    # RSSI is negative dBm — stronger = less negative = higher value
    # "significantly stronger than p95" means rssi > p95 + threshold * stddev
    if rssi > p95 + threshold_stddev * stddev:
        return _make_alert(
            "location",
            "medium",
            mac,
            f"Device {mac} RSSI {rssi} dBm much stronger than historical "
            f"p95={p95:.0f} dBm (stddev={stddev:.1f})",
            {
                "rssi": rssi,
                "rssi_p95": round(p95, 2),
                "rssi_stddev": round(stddev, 2),
                "rssi_mean": round(profile["rssi_mean"], 2) if profile["rssi_mean"] else None,
            },
        )
    return None


def detect_behavioral(
    conn: sqlite3.Connection,
    mac: str,
    timestamp: str,
    probe_rate_multiplier: float = 3.0,
) -> dict[str, Any] | None:
    """Check for behavioral anomalies in probing patterns.

    Fires if:
        1. Device is probing for SSIDs not in its historical set, OR
        2. Current probe rate exceeds historical mean by N times
    """
    profile = conn.execute(
        "SELECT probe_ssid_set, probe_rate_mean FROM device_profiles WHERE mac = ?",
        (mac,),
    ).fetchone()
    if profile is None:
        return None

    historical_ssids = set(json.loads(profile["probe_ssid_set"])) if profile["probe_ssid_set"] else set()
    historical_rate = profile["probe_rate_mean"] or 0

    # Check for new SSIDs in recent probes (last hour)
    try:
        ts = datetime.fromisoformat(timestamp)
        one_hour_ago = (ts - timedelta(hours=1)).isoformat()
    except (ValueError, TypeError):
        return None

    recent_probes = conn.execute(
        "SELECT DISTINCT ssid FROM probe_requests "
        "WHERE mac = ? AND ssid IS NOT NULL AND timestamp >= ?",
        (mac, one_hour_ago),
    ).fetchall()
    recent_ssids = {r["ssid"] for r in recent_probes}
    new_ssids = recent_ssids - historical_ssids

    if new_ssids and historical_ssids:
        return _make_alert(
            "behavioral",
            "low",
            mac,
            f"Device {mac} probing for {len(new_ssids)} new SSID(s): "
            f"{', '.join(sorted(new_ssids)[:5])}",
            {
                "new_ssids": sorted(new_ssids),
                "historical_ssids": sorted(historical_ssids),
            },
        )

    # Check probe rate (probes in last hour vs historical hourly mean)
    if historical_rate > 0:
        recent_count = conn.execute(
            "SELECT COUNT(*) FROM probe_requests WHERE mac = ? AND timestamp >= ?",
            (mac, one_hour_ago),
        ).fetchone()[0]

        if recent_count > historical_rate * probe_rate_multiplier:
            return _make_alert(
                "behavioral",
                "low",
                mac,
                f"Device {mac} probe rate {recent_count}/hr is "
                f"{recent_count / historical_rate:.1f}x historical mean ({historical_rate:.1f}/hr)",
                {
                    "recent_count": recent_count,
                    "historical_rate": round(historical_rate, 2),
                    "multiplier": round(recent_count / historical_rate, 2),
                },
            )

    return None


def detect_absence(
    conn: sqlite3.Connection,
    writer: DatabaseWriter,
    threshold_pct: float = 95.0,
    threshold_hours: float = 4.0,
) -> list[dict[str, Any]]:
    """Check for devices with high presence that have gone silent.

    Returns a list of alerts (may be empty).
    Runs as a periodic sweep, not per-event.
    """
    alerts: list[dict[str, Any]] = []

    profiles = conn.execute(
        "SELECT mac, presence_pct_30d FROM device_profiles WHERE presence_pct_30d >= ?",
        (threshold_pct,),
    ).fetchall()

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=threshold_hours)).isoformat()

    for profile in profiles:
        mac = profile["mac"]
        last_obs = conn.execute(
            "SELECT MAX(timestamp) as last_ts FROM observations WHERE mac = ?",
            (mac,),
        ).fetchone()

        if last_obs and last_obs["last_ts"] and last_obs["last_ts"] < cutoff:
            hours_absent = (
                datetime.now(timezone.utc) - datetime.fromisoformat(last_obs["last_ts"])
            ).total_seconds() / 3600

            alerts.append(_make_alert(
                "absence",
                "low",
                mac,
                f"High-presence device {mac} absent for {hours_absent:.1f}h "
                f"(presence={profile['presence_pct_30d']:.0f}%)",
                {
                    "last_seen": last_obs["last_ts"],
                    "hours_absent": round(hours_absent, 2),
                    "presence_pct": round(profile["presence_pct_30d"], 2),
                },
            ))

    return alerts


def detect_correlation(
    conn: sqlite3.Connection,
    mac: str,
    timestamp: str,
    companion_window_s: int = 60,
) -> dict[str, Any] | None:
    """Check if a device appears without its usual companions.

    Fires if the device is seen but none of its profiled companions
    are present within the companion window.
    """
    profile = conn.execute(
        "SELECT companion_macs FROM device_profiles WHERE mac = ?", (mac,)
    ).fetchone()
    if profile is None or not profile["companion_macs"]:
        return None

    companions = json.loads(profile["companion_macs"])
    if not companions:
        return None

    try:
        ts = datetime.fromisoformat(timestamp)
        ts_lo = (ts - timedelta(seconds=companion_window_s)).isoformat()
        ts_hi = (ts + timedelta(seconds=companion_window_s)).isoformat()
    except (ValueError, TypeError):
        return None

    # Check if any companion is present
    placeholders = ",".join("?" * len(companions))
    present = conn.execute(
        f"SELECT DISTINCT mac FROM observations "
        f"WHERE mac IN ({placeholders}) AND timestamp BETWEEN ? AND ?",
        (*companions, ts_lo, ts_hi),
    ).fetchall()

    present_macs = {r["mac"] for r in present}
    missing = set(companions) - present_macs

    if missing and len(missing) == len(companions):
        return _make_alert(
            "correlation",
            "info",
            mac,
            f"Device {mac} appeared without any of its {len(companions)} "
            f"usual companion(s)",
            {
                "expected_companions": companions,
                "present_companions": sorted(present_macs),
                "missing_companions": sorted(missing),
            },
        )
    return None


def detect_probe_set_cluster(
    conn: sqlite3.Connection,
    mac: str,
    timestamp: str,
) -> dict[str, Any] | None:
    """Check if a randomized MAC belongs to a known probe cluster.

    Fires info on new cluster membership, medium if the cluster
    appears at an anomalous time/place.
    """
    membership = conn.execute(
        "SELECT pcm.cluster_id, pcm.jaccard_score, pc.ssid_set "
        "FROM probe_cluster_members pcm "
        "JOIN probe_clusters pc ON pcm.cluster_id = pc.cluster_id "
        "WHERE pcm.mac = ?",
        (mac,),
    ).fetchone()

    if membership is None:
        return None

    cluster_id = membership["cluster_id"]
    ssid_set = json.loads(membership["ssid_set"])

    # Check if cluster has been seen before at this time of day
    try:
        ts = datetime.fromisoformat(timestamp)
        current_hour = ts.hour
    except (ValueError, TypeError):
        return None

    # Get all members of this cluster
    members = conn.execute(
        "SELECT mac FROM probe_cluster_members WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchall()
    member_macs = [m["mac"] for m in members]

    # Check historical time-of-day for any cluster member
    has_temporal_history = False
    for m_mac in member_macs:
        profile = conn.execute(
            "SELECT time_histogram FROM device_profiles WHERE mac = ?", (m_mac,)
        ).fetchone()
        if profile and profile["time_histogram"]:
            hist = json.loads(profile["time_histogram"])
            if hist[current_hour] > 0:
                has_temporal_history = True
                break

    if not has_temporal_history and any(
        conn.execute(
            "SELECT 1 FROM device_profiles WHERE mac = ?", (m,)
        ).fetchone() is not None
        for m in member_macs
    ):
        # Cluster exists with profiles but never seen at this hour
        return _make_alert(
            "probe_set_cluster",
            "medium",
            mac,
            f"Probe cluster {cluster_id[:8]}... ({len(member_macs)} MACs) "
            f"active at unusual hour {current_hour:02d}:00",
            {
                "cluster_id": cluster_id,
                "member_count": len(member_macs),
                "ssid_set": ssid_set,
                "hour": current_hour,
                "mac": mac,
            },
        )

    # Default: info-level alert for cluster activity
    return _make_alert(
        "probe_set_cluster",
        "info",
        mac,
        f"Probe cluster {cluster_id[:8]}... ({len(member_macs)} MACs, "
        f"SSIDs: {', '.join(ssid_set[:3])})",
        {
            "cluster_id": cluster_id,
            "member_count": len(member_macs),
            "ssid_set": ssid_set,
            "jaccard_score": round(membership["jaccard_score"], 4),
        },
    )


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_observation(
    conn: sqlite3.Connection,
    writer: DatabaseWriter,
    event: dict[str, Any],
    learning: bool,
    cfg: Any,
) -> list[dict[str, Any]]:
    """Score a single observation against all anomaly detectors.

    Args:
        conn: Read-only DB connection.
        writer: DB writer for alert output.
        event: Observation dict (from the observations table or live).
        learning: Whether learning mode is active.
        cfg: SentinelConfig for thresholds.

    Returns:
        List of alert dicts generated (also written to DB).
    """
    mac = event.get("mac", "")
    timestamp = event.get("timestamp", "")
    rssi = event.get("rssi")
    det = cfg.detection
    alerts: list[dict[str, Any]] = []

    # new_device — always runs, even during learning
    alert = detect_new_device(conn, mac, learning)
    if alert:
        alerts.append(alert)

    # During learning mode, skip all other detectors
    if learning:
        for a in alerts:
            _write_alert(writer, a)
        return alerts

    # temporal
    alert = detect_temporal(conn, mac, timestamp, det.temporal_stddev)
    if alert:
        alerts.append(alert)

    # location
    alert = detect_location(conn, mac, rssi, det.location_stddev)
    if alert:
        alerts.append(alert)

    # behavioral
    alert = detect_behavioral(conn, mac, timestamp, det.behavioral_probe_rate_multiplier)
    if alert:
        alerts.append(alert)

    # correlation
    alert = detect_correlation(
        conn, mac, timestamp, cfg.profiler.companion_window_s
    )
    if alert:
        alerts.append(alert)

    # probe_set_cluster
    alert = detect_probe_set_cluster(conn, mac, timestamp)
    if alert:
        alerts.append(alert)

    # Write all alerts
    for a in alerts:
        _write_alert(writer, a)

    return alerts


# ---------------------------------------------------------------------------
# Detection daemon
# ---------------------------------------------------------------------------

class DetectionDaemon:
    """Live detection daemon that polls for new observations and scores them."""

    def __init__(self, config_path: str | None = None) -> None:
        if config_path:
            load_config(config_path)
        self._cfg = get_config()
        self._running = True
        self._last_obs_id: int = 0
        self._alert_count: int = 0
        self._absence_check_interval: int = 300  # check absence every 5 minutes
        self._last_absence_check: float = 0

    async def run(self) -> None:
        """Main daemon loop."""
        install_sighup_handler()
        db_path = self._cfg.resolved_db_path

        writer = DatabaseWriter(db_path)
        writer.start()

        # Install signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown, sig)

        logger.info("Detection daemon started")

        try:
            while self._running:
                conn = get_readonly_connection(db_path)
                try:
                    learning = is_learning_mode(conn, self._cfg.learning_period_days)
                    if learning:
                        logger.debug("Learning mode active")

                    # Poll for new observations
                    new_obs = conn.execute(
                        "SELECT id, timestamp, mac, source, rssi, channel "
                        "FROM observations WHERE id > ? ORDER BY id LIMIT 100",
                        (self._last_obs_id,),
                    ).fetchall()

                    for obs in new_obs:
                        event = dict(obs)
                        alerts = score_observation(conn, writer, event, learning, self._cfg)
                        self._alert_count += len(alerts)
                        for a in alerts:
                            logger.info(
                                "[%s] %s: %s",
                                a["severity"].upper(), a["alert_type"], a["summary"],
                            )
                        self._last_obs_id = obs["id"]

                    # Periodic absence check (not per-event)
                    now = _time.monotonic()
                    if not learning and (now - self._last_absence_check) >= self._absence_check_interval:
                        absence_alerts = detect_absence(
                            conn, writer,
                            self._cfg.detection.absence_presence_pct,
                            self._cfg.detection.absence_hours,
                        )
                        for a in absence_alerts:
                            _write_alert(writer, a)
                            self._alert_count += 1
                            logger.info(
                                "[%s] %s: %s",
                                a["severity"].upper(), a["alert_type"], a["summary"],
                            )
                        self._last_absence_check = now

                finally:
                    conn.close()

                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            pass
        finally:
            writer.stop()
            logger.info("Detection daemon stopped. Total alerts: %d", self._alert_count)

    def _shutdown(self, sig: signal.Signals) -> None:
        logger.info("Received %s, shutting down", sig.name)
        self._running = False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main(args: Any) -> None:
    cfg = load_config(args.config)
    from sentinel.common.logging import setup_logging
    setup_logging("detector")

    from sentinel.config import validate_config
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        import sys
        sys.exit(1)

    daemon = DetectionDaemon()
    await daemon.run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel detection daemon")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config.yaml")
    parsed = parser.parse_args()

    asyncio.run(main(parsed))
