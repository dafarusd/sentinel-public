"""Profiler engine for Sentinel.

Computes per-device statistical profiles from observation history:
    - Time-of-day presence histogram (24 hourly bins)
    - RSSI statistics (mean, stddev, p95)
    - Channel set (distinct channels observed on)
    - Probe SSID set (SSIDs from probe requests)
    - Probe rate (probes per hour)
    - Companion devices (co-present within configurable window)
    - Presence percentage (hours present in last 30 days)

Also runs Level B probe-set clustering: groups randomized MACs that
probe for overlapping SSID sets (Jaccard similarity > threshold).

Designed to run every 15 minutes via systemd timer, or on demand.

Usage:
    python -m sentinel.profiler.engine
    python -m sentinel.profiler.engine --config /path/to/config.yaml
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sentinel.common.oui import is_locally_administered
from sentinel.config import get_config, load_config
from sentinel.db.writer import DatabaseWriter, get_readonly_connection

logger = logging.getLogger("sentinel.profiler")


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float:
    """Arithmetic mean, or 0 for empty list."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _stddev(values: list[float], mean_val: float | None = None) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    if mean_val is None:
        mean_val = _mean(values)
    variance = sum((x - mean_val) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute percentile from a sorted list (linear interpolation)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100.0) * (len(sorted_values) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Jaccard similarity coefficient between two sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Profile computation
# ---------------------------------------------------------------------------

def compute_device_profile(
    conn: sqlite3.Connection,
    mac: str,
    companion_window_s: int = 60,
    min_observations: int = 10,
) -> dict[str, Any] | None:
    """Compute a statistical profile for a single device.

    Args:
        conn: Read-only DB connection.
        mac: Device MAC address.
        companion_window_s: Co-presence window in seconds.
        min_observations: Minimum observations required to build a profile.

    Returns:
        Profile dict ready for DB insertion, or None if insufficient data.
    """
    # Fetch all observations for this device
    rows = conn.execute(
        "SELECT timestamp, rssi, channel FROM observations WHERE mac = ? ORDER BY timestamp",
        (mac,),
    ).fetchall()

    if len(rows) < min_observations:
        return None

    now = datetime.now(timezone.utc)

    # --- Time-of-day histogram (24 bins) ---
    time_histogram = [0] * 24
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["timestamp"])
            time_histogram[ts.hour] += 1
        except (ValueError, TypeError):
            pass

    # --- RSSI statistics ---
    rssi_values = [row["rssi"] for row in rows if row["rssi"] is not None]
    rssi_sorted = sorted(rssi_values)
    rssi_m = _mean(rssi_values)
    rssi_sd = _stddev(rssi_values, rssi_m)
    rssi_p95 = _percentile(rssi_sorted, 95.0)

    # --- Channel set ---
    channels = sorted({row["channel"] for row in rows if row["channel"] is not None})

    # --- Probe SSID set ---
    probe_rows = conn.execute(
        "SELECT DISTINCT ssid FROM probe_requests WHERE mac = ? AND ssid IS NOT NULL",
        (mac,),
    ).fetchall()
    probe_ssids = sorted({r["ssid"] for r in probe_rows})

    # --- Probe rate (probes per hour) ---
    probe_count = conn.execute(
        "SELECT COUNT(*) FROM probe_requests WHERE mac = ?", (mac,)
    ).fetchone()[0]

    # Time span in hours
    try:
        first_ts = datetime.fromisoformat(rows[0]["timestamp"])
        last_ts = datetime.fromisoformat(rows[-1]["timestamp"])
        span_hours = max((last_ts - first_ts).total_seconds() / 3600, 1.0)
    except (ValueError, TypeError):
        span_hours = 1.0
    probe_rate_mean = probe_count / span_hours

    # --- Companion devices (co-present within window) ---
    companion_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        ts_str = row["timestamp"]
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        ts_lo = (ts - timedelta(seconds=companion_window_s)).isoformat()
        ts_hi = (ts + timedelta(seconds=companion_window_s)).isoformat()

        companions = conn.execute(
            "SELECT DISTINCT mac FROM observations "
            "WHERE mac != ? AND timestamp BETWEEN ? AND ? LIMIT 50",
            (mac, ts_lo, ts_hi),
        ).fetchall()
        for c in companions:
            companion_counts[c["mac"]] += 1

    # Keep companions seen in >10% of this device's observations
    threshold = max(len(rows) * 0.1, 3)
    companion_macs = sorted([
        m for m, count in companion_counts.items() if count >= threshold
    ])

    # --- Presence percentage (last 30 days) ---
    thirty_days_ago = (now - timedelta(days=30)).isoformat()
    hours_with_obs = conn.execute(
        "SELECT COUNT(DISTINCT strftime('%Y-%m-%d %H', timestamp)) "
        "FROM observations WHERE mac = ? AND timestamp >= ?",
        (mac, thirty_days_ago),
    ).fetchone()[0]
    # 30 days * 24 hours = 720 total possible hours
    presence_pct = (hours_with_obs / 720.0) * 100.0

    return {
        "mac": mac,
        "updated_at": now.isoformat(),
        "time_histogram": json.dumps(time_histogram),
        "rssi_mean": round(rssi_m, 2) if rssi_values else None,
        "rssi_stddev": round(rssi_sd, 2) if rssi_values else None,
        "rssi_p95": round(rssi_p95, 2) if rssi_values else None,
        "channel_set": json.dumps(channels),
        "probe_ssid_set": json.dumps(probe_ssids),
        "probe_rate_mean": round(probe_rate_mean, 4),
        "companion_macs": json.dumps(companion_macs),
        "presence_pct_30d": round(presence_pct, 2),
        "total_observations": len(rows),
    }


# ---------------------------------------------------------------------------
# Probe-set clustering (Level B)
# ---------------------------------------------------------------------------

def compute_probe_clusters(
    conn: sqlite3.Connection,
    jaccard_threshold: float = 0.6,
) -> list[dict[str, Any]]:
    """Cluster randomized MACs by overlapping probe SSID sets.

    Level B: groups locally-administered MACs whose probe-request SSID sets
    have Jaccard similarity above threshold.

    Args:
        conn: Read-only DB connection.
        jaccard_threshold: Minimum Jaccard similarity to cluster.

    Returns:
        List of cluster dicts with members and scores.
    """
    # Get all locally-administered MACs that have probed for at least 2 SSIDs
    rows = conn.execute(
        "SELECT mac, GROUP_CONCAT(DISTINCT ssid) as ssids "
        "FROM probe_requests "
        "WHERE ssid IS NOT NULL "
        "GROUP BY mac "
        "HAVING COUNT(DISTINCT ssid) >= 2"
    ).fetchall()

    # Filter to locally-administered (randomized) MACs only
    mac_ssids: dict[str, set[str]] = {}
    for row in rows:
        mac = row["mac"]
        if is_locally_administered(mac):
            ssids = set(row["ssids"].split(","))
            mac_ssids[mac] = ssids

    if len(mac_ssids) < 2:
        return []

    # Pairwise Jaccard comparison and union-find clustering
    macs = list(mac_ssids.keys())
    parent: dict[str, str] = {m: m for m in macs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Track pairwise scores
    scores: dict[tuple[str, str], float] = {}

    for i in range(len(macs)):
        for j in range(i + 1, len(macs)):
            score = _jaccard(mac_ssids[macs[i]], mac_ssids[macs[j]])
            if score >= jaccard_threshold:
                union(macs[i], macs[j])
                scores[(macs[i], macs[j])] = score
                scores[(macs[j], macs[i])] = score

    # Group by cluster root
    clusters_map: dict[str, list[str]] = defaultdict(list)
    for m in macs:
        clusters_map[find(m)].append(m)

    # Only keep clusters with 2+ members
    now = datetime.now(timezone.utc).isoformat()
    clusters: list[dict[str, Any]] = []

    for root, members in clusters_map.items():
        if len(members) < 2:
            continue

        # Cluster SSID set = union of all member SSID sets
        cluster_ssids = set()
        for m in members:
            cluster_ssids |= mac_ssids[m]

        cluster_id = str(uuid.uuid4())

        member_records = []
        for m in members:
            # Best score against any other member in this cluster
            best_score = max(
                (scores.get((m, other), 0.0) for other in members if other != m),
                default=0.0,
            )
            member_records.append({
                "cluster_id": cluster_id,
                "mac": m,
                "joined_at": now,
                "jaccard_score": round(best_score, 4),
            })

        clusters.append({
            "cluster_id": cluster_id,
            "created_at": now,
            "updated_at": now,
            "ssid_set": json.dumps(sorted(cluster_ssids)),
            "device_count": len(members),
            "members": member_records,
        })

    return clusters


# ---------------------------------------------------------------------------
# IE-fingerprint clustering (Stage 14b)
# ---------------------------------------------------------------------------

# Temporal window for grouping MACs that share an IE fingerprint. Two MACs
# only cluster under IE-fingerprint evidence if all cluster members fall
# within a rolling window of this length, sorted by first_seen. Prevents
# collapsing identical-model devices observed months apart.
_IE_CLUSTER_WINDOW_HOURS = 24

# Evidence-type tag written into probe_clusters.evidence_type for rows
# produced by this path. The existing SSID-Jaccard path relies on the
# column DEFAULT ('ssid_jaccard') and does not pass the column explicitly.
_EVIDENCE_IE = "ie_fingerprint"

# Stage 14d: BLE manufacturer-data clustering. Same temporal-window rule
# as IE clustering to prevent collapsing identical-model devices observed
# months apart.
_BLE_CLUSTER_WINDOW_HOURS = 24
_EVIDENCE_BLE = "ble_mfr_data"

# Stage 14e: BLE service-UUID clustering. Same temporal-window discipline.
# Stage 14f: cross-modality co-presence clustering (graph-based, no window).
_EVIDENCE_SERVICE_UUID = "service_uuid"
_EVIDENCE_COPRESENCE = "copresence"

# Placeholder score for IE-cluster members. Schema requires
# probe_cluster_members.jaccard_score NOT NULL; a shared exact IE
# fingerprint is as strong as Jaccard can express, so 1.0 is honest.
# Multi-evidence scoring (Stage 14c) will replace this with a real
# fused confidence value. Same reasoning applies to BLE clusters.
_IE_PLACEHOLDER_SCORE = 1.0


def _bucket_by_window(
    macs: list[tuple[str, str, str]], window_hours: int
) -> list[list[tuple[str, str, str]]]:
    """Greedy-bucket (mac, first_seen, last_seen) rows into temporal windows.

    Input is expected sorted ascending by first_seen. A new bucket opens at
    the earliest MAC not yet placed; subsequent MACs join that bucket as
    long as their first_seen is within ``window_hours`` of the bucket's
    earliest first_seen. Once a MAC falls outside that window, it seeds the
    next bucket.

    Malformed timestamps are placed in a separate terminal bucket so the
    main grouping logic never crashes on bad data.
    """
    buckets: list[list[tuple[str, str, str]]] = []
    current: list[tuple[str, str, str]] = []
    anchor: datetime | None = None
    window = timedelta(hours=window_hours)

    for entry in macs:
        _mac, first_seen, _last_seen = entry
        try:
            first_ts = datetime.fromisoformat(first_seen)
        except (ValueError, TypeError):
            # Unparseable timestamp: stash separately, don't let it corrupt
            # window math. A single malformed row shouldn't merge or split
            # legitimate clusters.
            buckets.append([entry])
            continue

        if anchor is None:
            anchor = first_ts
            current = [entry]
        elif first_ts - anchor <= window:
            current.append(entry)
        else:
            buckets.append(current)
            anchor = first_ts
            current = [entry]

    if current:
        buckets.append(current)

    return buckets


def compute_ie_clusters(
    conn: sqlite3.Connection,
    window_hours: int = _IE_CLUSTER_WINDOW_HOURS,
) -> list[dict[str, Any]]:
    """Cluster MACs by shared 802.11 IE fingerprint within a temporal window.

    For every ``ie_fingerprint_hash`` that covers ≥2 distinct MACs, the
    candidate MACs are sorted by first_seen and greedy-bucketed into
    ``window_hours`` windows. Each bucket with ≥2 MACs becomes a cluster.

    Args:
        conn: Read-only DB connection.
        window_hours: Maximum first-seen span allowed inside a single
            cluster. Defaults to 24 hours.

    Returns:
        List of cluster dicts ready for persistence. Shape is compatible
        with the existing SSID-Jaccard clusters (cluster_id, created_at,
        updated_at, ssid_set, device_count, members), with an added
        evidence_type field and an ie_fingerprint_hash field identifying
        the fingerprint that drove the cluster.
    """
    rows = conn.execute(
        "SELECT ie_fingerprint_hash AS fp, mac, "
        "       MIN(timestamp) AS first_seen, "
        "       MAX(timestamp) AS last_seen "
        "FROM probe_requests "
        "WHERE ie_fingerprint_hash IS NOT NULL "
        "GROUP BY ie_fingerprint_hash, mac "
        "ORDER BY ie_fingerprint_hash, first_seen"
    ).fetchall()

    # Group (mac, first_seen, last_seen) tuples by fingerprint hash.
    by_fp: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in rows:
        by_fp[row["fp"]].append((row["mac"], row["first_seen"], row["last_seen"]))

    now = datetime.now(timezone.utc).isoformat()
    clusters: list[dict[str, Any]] = []

    for fp_hash, mac_rows in by_fp.items():
        if len(mac_rows) < 2:
            continue  # single-MAC fingerprints are not clusters

        # Rows are already first_seen-sorted by the SQL ORDER BY within
        # each fingerprint group.
        for bucket in _bucket_by_window(mac_rows, window_hours):
            if len(bucket) < 2:
                continue

            cluster_id = str(uuid.uuid4())
            first_seen = min(entry[1] for entry in bucket)
            last_seen = max(entry[2] for entry in bucket)

            member_records = [
                {
                    "cluster_id": cluster_id,
                    "mac": mac,
                    "joined_at": now,
                    "jaccard_score": _IE_PLACEHOLDER_SCORE,
                }
                for mac, _fs, _ls in bucket
            ]

            clusters.append({
                "cluster_id": cluster_id,
                "created_at": now,
                "updated_at": now,
                # ssid_set is NOT NULL in schema; IE clusters have no
                # SSID-defining set, so we emit an empty JSON array.
                "ssid_set": json.dumps([]),
                "device_count": len(bucket),
                "evidence_type": _EVIDENCE_IE,
                "ie_fingerprint_hash": fp_hash,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "members": member_records,
            })

    return clusters


def _persist_ie_clusters(
    db_path: Path, clusters: list[dict[str, Any]]
) -> None:
    """Atomically replace all IE-fingerprint clusters in one transaction.

    Opens a short-lived direct sqlite3 connection (bypassing the writer
    thread) so we can wrap DELETE + INSERT in a single BEGIN IMMEDIATE
    transaction. The existing DatabaseWriter commits per statement, which
    can't give us cross-statement atomicity.

    Only touches rows where evidence_type='ie_fingerprint'. Rows written
    by the SSID-Jaccard path (evidence_type='ssid_jaccard') are left alone.
    devices.probe_cluster_id is deliberately NOT modified here — Stage 14c
    will own multi-evidence fusion and final cluster assignment.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Purge previous IE clusters. Members first (FK would complain
            # if ordering were reversed under STRICT foreign-key enforcement).
            conn.execute(
                "DELETE FROM probe_cluster_members "
                "WHERE cluster_id IN ("
                "    SELECT cluster_id FROM probe_clusters "
                "    WHERE evidence_type = ?"
                ")",
                (_EVIDENCE_IE,),
            )
            conn.execute(
                "DELETE FROM probe_clusters WHERE evidence_type = ?",
                (_EVIDENCE_IE,),
            )

            for cluster in clusters:
                conn.execute(
                    "INSERT INTO probe_clusters "
                    "(cluster_id, created_at, updated_at, ssid_set, "
                    " device_count, evidence_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cluster["cluster_id"],
                        cluster["created_at"],
                        cluster["updated_at"],
                        cluster["ssid_set"],
                        cluster["device_count"],
                        cluster["evidence_type"],
                    ),
                )
                conn.executemany(
                    "INSERT INTO probe_cluster_members "
                    "(cluster_id, mac, joined_at, jaccard_score) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            m["cluster_id"],
                            m["mac"],
                            m["joined_at"],
                            m["jaccard_score"],
                        )
                        for m in cluster["members"]
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BLE manufacturer-data clustering (Stage 14d)
# ---------------------------------------------------------------------------

def compute_ble_clusters(
    conn: sqlite3.Connection,
    window_hours: int = _BLE_CLUSTER_WINDOW_HOURS,
) -> list[dict[str, Any]]:
    """Cluster MACs by shared BLE manufacturer-data fingerprint within window.

    Parallels ``compute_ie_clusters`` but keyed on
    ``bt_advertisements.mfr_fingerprint_hash``. For every fingerprint hash
    covering ≥2 distinct MACs, the candidate MACs are sorted by first_seen
    and greedy-bucketed into ``window_hours`` windows. Each bucket with
    ≥2 MACs becomes a cluster.

    Args:
        conn: Read-only DB connection.
        window_hours: Maximum first-seen span allowed inside a single
            cluster. Defaults to 24 hours.

    Returns:
        List of cluster dicts compatible with the IE-cluster shape
        (cluster_id, created_at, updated_at, ssid_set='[]', device_count,
        evidence_type='ble_mfr_data', members).
    """
    rows = conn.execute(
        "SELECT mfr_fingerprint_hash AS fp, mac, "
        "       MIN(timestamp) AS first_seen, "
        "       MAX(timestamp) AS last_seen "
        "FROM bt_advertisements "
        "WHERE mfr_fingerprint_hash IS NOT NULL "
        "GROUP BY mfr_fingerprint_hash, mac "
        "ORDER BY mfr_fingerprint_hash, first_seen"
    ).fetchall()

    by_fp: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in rows:
        by_fp[row["fp"]].append((row["mac"], row["first_seen"], row["last_seen"]))

    now = datetime.now(timezone.utc).isoformat()
    clusters: list[dict[str, Any]] = []

    for fp_hash, mac_rows in by_fp.items():
        if len(mac_rows) < 2:
            continue  # single-MAC fingerprints are not clusters

        for bucket in _bucket_by_window(mac_rows, window_hours):
            if len(bucket) < 2:
                continue

            cluster_id = str(uuid.uuid4())
            first_seen = min(entry[1] for entry in bucket)
            last_seen = max(entry[2] for entry in bucket)

            member_records = [
                {
                    "cluster_id": cluster_id,
                    "mac": mac,
                    "joined_at": now,
                    "jaccard_score": _IE_PLACEHOLDER_SCORE,
                }
                for mac, _fs, _ls in bucket
            ]

            clusters.append({
                "cluster_id": cluster_id,
                "created_at": now,
                "updated_at": now,
                # ssid_set is NOT NULL in schema; BLE clusters have no
                # SSID-defining set, so we emit an empty JSON array.
                "ssid_set": json.dumps([]),
                "device_count": len(bucket),
                "evidence_type": _EVIDENCE_BLE,
                "mfr_fingerprint_hash": fp_hash,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "members": member_records,
            })

    return clusters


def _persist_ble_clusters(
    db_path: Path, clusters: list[dict[str, Any]]
) -> None:
    """Atomically replace all BLE-mfr-data clusters in one transaction.

    Mirrors ``_persist_ie_clusters`` exactly — same direct sqlite3 connect,
    same BEGIN IMMEDIATE wrap. Only touches rows where
    evidence_type='ble_mfr_data'; IE-fingerprint and SSID-Jaccard clusters
    are left alone. devices.probe_cluster_id is deliberately NOT modified
    here — Stage 14c will own multi-evidence fusion and final cluster
    assignment.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM probe_cluster_members "
                "WHERE cluster_id IN ("
                "    SELECT cluster_id FROM probe_clusters "
                "    WHERE evidence_type = ?"
                ")",
                (_EVIDENCE_BLE,),
            )
            conn.execute(
                "DELETE FROM probe_clusters WHERE evidence_type = ?",
                (_EVIDENCE_BLE,),
            )

            for cluster in clusters:
                conn.execute(
                    "INSERT INTO probe_clusters "
                    "(cluster_id, created_at, updated_at, ssid_set, "
                    " device_count, evidence_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cluster["cluster_id"],
                        cluster["created_at"],
                        cluster["updated_at"],
                        cluster["ssid_set"],
                        cluster["device_count"],
                        cluster["evidence_type"],
                    ),
                )
                conn.executemany(
                    "INSERT INTO probe_cluster_members "
                    "(cluster_id, mac, joined_at, jaccard_score) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            m["cluster_id"],
                            m["mac"],
                            m["joined_at"],
                            m["jaccard_score"],
                        )
                        for m in cluster["members"]
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Service-UUID clustering (Stage 14e)
# ---------------------------------------------------------------------------

_SERVICE_UUID_CLUSTER_WINDOW_HOURS = 24


def compute_service_uuid_clusters(
    conn: sqlite3.Connection,
    window_hours: int = _SERVICE_UUID_CLUSTER_WINDOW_HOURS,
) -> list[dict[str, Any]]:
    """Cluster BT MACs by shared service UUID set within a temporal window.

    Parallels compute_ble_clusters() but keys on bt_advertisements.service_uuids.
    Service UUIDs are stored as JSON arrays of UUID strings; we normalize by
    sorting and joining to produce a stable grouping key.

    For every normalized UUID-set covering >=2 distinct MACs, candidate MACs
    are sorted by first_seen and greedy-bucketed into window_hours windows.
    Each bucket with >=2 MACs becomes a cluster.

    Args:
        conn: Read-only DB connection.
        window_hours: Maximum first-seen span allowed inside a single cluster.

    Returns:
        List of cluster dicts compatible with the existing probe_clusters
        shape (cluster_id, created_at, updated_at, ssid_set, device_count,
        evidence_type='service_uuid', members).
    """
    rows = conn.execute(
        "SELECT service_uuids AS uuids, mac, "
        "       MIN(timestamp) AS first_seen, "
        "       MAX(timestamp) AS last_seen "
        "FROM bt_advertisements "
        "WHERE service_uuids IS NOT NULL AND service_uuids != '[]' "
        "GROUP BY service_uuids, mac "
        "ORDER BY service_uuids, first_seen"
    ).fetchall()

    # Normalize UUID set: parse JSON array, sort, rejoin. This makes
    # ["A","B"] and ["B","A"] cluster identically.
    by_uuid_set: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in rows:
        try:
            uuids = json.loads(row["uuids"])
            if not isinstance(uuids, list) or not uuids:
                continue
            normalized = json.dumps(sorted(uuids))
        except (json.JSONDecodeError, TypeError):
            continue
        by_uuid_set[normalized].append(
            (row["mac"], row["first_seen"], row["last_seen"])
        )

    now = datetime.now(timezone.utc).isoformat()
    clusters: list[dict[str, Any]] = []

    for uuid_set, mac_rows in by_uuid_set.items():
        if len(mac_rows) < 2:
            continue  # single-MAC UUID sets are not clusters

        for bucket in _bucket_by_window(mac_rows, window_hours):
            if len(bucket) < 2:
                continue

            cluster_id = f"svc-{hashlib.sha256(uuid_set.encode()).hexdigest()[:16]}-{bucket[0][1][:10]}"

            members = [
                {
                    "cluster_id": cluster_id,
                    "mac": mac,
                    "joined_at": first_seen,
                    "jaccard_score": 1.0,  # exact UUID-set match
                }
                for (mac, first_seen, _last_seen) in bucket
            ]

            clusters.append({
                "cluster_id": cluster_id,
                "created_at": now,
                "updated_at": now,
                "ssid_set": uuid_set,  # store normalized UUID JSON in ssid_set field
                "device_count": len(bucket),
                "evidence_type": _EVIDENCE_SERVICE_UUID,
                "members": members,
            })

    return clusters


def _persist_service_uuid_clusters(
    db_path: Path, clusters: list[dict[str, Any]]
) -> None:
    """Atomically replace all service_uuid clusters in one transaction.

    Same wipe-and-rebuild semantics as _persist_ie_clusters(), scoped to
    evidence_type='service_uuid'. Other cluster types are untouched.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM probe_cluster_members "
                "WHERE cluster_id IN ("
                "    SELECT cluster_id FROM probe_clusters "
                "    WHERE evidence_type = ?"
                ")",
                (_EVIDENCE_SERVICE_UUID,),
            )
            conn.execute(
                "DELETE FROM probe_clusters WHERE evidence_type = ?",
                (_EVIDENCE_SERVICE_UUID,),
            )

            for cluster in clusters:
                conn.execute(
                    "INSERT INTO probe_clusters "
                    "(cluster_id, created_at, updated_at, ssid_set, "
                    " device_count, evidence_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cluster["cluster_id"],
                        cluster["created_at"],
                        cluster["updated_at"],
                        cluster["ssid_set"],
                        cluster["device_count"],
                        cluster["evidence_type"],
                    ),
                )
                conn.executemany(
                    "INSERT INTO probe_cluster_members "
                    "(cluster_id, mac, joined_at, jaccard_score) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (m["cluster_id"], m["mac"], m["joined_at"], m["jaccard_score"])
                        for m in cluster["members"]
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-modality co-presence clustering (Stage 14f)
# ---------------------------------------------------------------------------

_COPRESENCE_MIN_PROFILE_OBS = 50  # require enough data for meaningful companion stats


def compute_copresence_clusters(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Cluster MACs that mutually list each other as companions.

    Reads device_profiles.companion_macs (already computed by
    compute_device_profile). Forms an undirected graph from pairs where
    A->B AND B->A are both present in each other's companion lists.
    Connected components in that graph are clusters.

    First cross-modality cluster type in Sentinel: a WiFi MAC and a BT MAC
    will cluster if they consistently co-occur within the companion window.

    Args:
        conn: Read-only DB connection.

    Returns:
        List of cluster dicts with evidence_type='copresence'.
    """
    rows = conn.execute(
        "SELECT mac, companion_macs, total_observations "
        "FROM device_profiles "
        "WHERE companion_macs IS NOT NULL AND companion_macs != '[]' "
        "AND total_observations >= ?",
        (_COPRESENCE_MIN_PROFILE_OBS,),
    ).fetchall()

    # Build companion map: mac -> set of companion macs
    companions: dict[str, set[str]] = {}
    for row in rows:
        try:
            comp_list = json.loads(row["companion_macs"])
            if isinstance(comp_list, list):
                companions[row["mac"]] = set(comp_list)
        except (json.JSONDecodeError, TypeError):
            continue

    if len(companions) < 2:
        return []

    # Union-find over bidirectional companion pairs
    parent: dict[str, str] = {m: m for m in companions}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Edge condition: bidirectional companionship
    for mac_a, comps_a in companions.items():
        for mac_b in comps_a:
            if mac_b in companions and mac_a in companions[mac_b]:
                union(mac_a, mac_b)

    # Group by root
    groups: dict[str, list[str]] = defaultdict(list)
    for mac in companions:
        groups[find(mac)].append(mac)

    now = datetime.now(timezone.utc).isoformat()
    clusters: list[dict[str, Any]] = []

    for root, members in groups.items():
        if len(members) < 2:
            continue

        cluster_id = f"copres-{hashlib.sha256(root.encode()).hexdigest()[:16]}"
        member_records = [
            {
                "cluster_id": cluster_id,
                "mac": mac,
                "joined_at": now,
                "jaccard_score": 1.0,  # bidirectional companion = exact match
            }
            for mac in sorted(members)
        ]

        clusters.append({
            "cluster_id": cluster_id,
            "created_at": now,
            "updated_at": now,
            "ssid_set": json.dumps(sorted(members)),  # store member MACs in ssid_set
            "device_count": len(members),
            "evidence_type": _EVIDENCE_COPRESENCE,
            "members": member_records,
        })

    return clusters


def _persist_copresence_clusters(
    db_path: Path, clusters: list[dict[str, Any]]
) -> None:
    """Atomically replace all copresence clusters. Same pattern as IE/BLE."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM probe_cluster_members "
                "WHERE cluster_id IN ("
                "    SELECT cluster_id FROM probe_clusters "
                "    WHERE evidence_type = ?"
                ")",
                (_EVIDENCE_COPRESENCE,),
            )
            conn.execute(
                "DELETE FROM probe_clusters WHERE evidence_type = ?",
                (_EVIDENCE_COPRESENCE,),
            )

            for cluster in clusters:
                conn.execute(
                    "INSERT INTO probe_clusters "
                    "(cluster_id, created_at, updated_at, ssid_set, "
                    " device_count, evidence_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cluster["cluster_id"],
                        cluster["created_at"],
                        cluster["updated_at"],
                        cluster["ssid_set"],
                        cluster["device_count"],
                        cluster["evidence_type"],
                    ),
                )
                conn.executemany(
                    "INSERT INTO probe_cluster_members "
                    "(cluster_id, mac, joined_at, jaccard_score) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (m["cluster_id"], m["mac"], m["joined_at"], m["jaccard_score"])
                        for m in cluster["members"]
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main profiler run
# ---------------------------------------------------------------------------

def run_profiler(writer: DatabaseWriter | None = None, db_path: Path | None = None) -> dict[str, int]:
    """Run a full profiler cycle.

    Computes profiles for all devices with sufficient observations,
    then runs probe-set clustering.

    Args:
        writer: DatabaseWriter to use for writes. If None, creates one.
        db_path: Override DB path. If None, uses config.

    Returns:
        Stats dict with counts of profiles updated, clusters found, etc.
    """
    cfg = get_config()
    if db_path is None:
        db_path = cfg.resolved_db_path

    own_writer = writer is None
    if own_writer:
        writer = DatabaseWriter(db_path)
        writer.start()

    conn = get_readonly_connection(db_path)
    stats = {
        "profiles_updated": 0,
        "profiles_skipped": 0,
        "clusters_found": 0,
        "ie_clusters_found": 0,
        "ie_cluster_members": 0,
        "ble_clusters_found": 0,
        "ble_cluster_members": 0,
        "service_uuid_clusters_found": 0,
        "service_uuid_cluster_members": 0,
        "copresence_clusters_found": 0,
        "copresence_cluster_members": 0,
    }

    try:
        # Get all known device MACs
        device_macs = [
            row["mac"]
            for row in conn.execute("SELECT mac FROM devices").fetchall()
        ]
        logger.info("Profiling %d devices", len(device_macs))

        # Compute profiles
        for mac in device_macs:
            profile = compute_device_profile(
                conn,
                mac,
                companion_window_s=cfg.profiler.companion_window_s,
                min_observations=cfg.profiler.min_observations,
            )
            if profile is None:
                stats["profiles_skipped"] += 1
                continue

            writer.execute(
                "INSERT OR REPLACE INTO device_profiles "
                "(mac, updated_at, time_histogram, rssi_mean, rssi_stddev, rssi_p95, "
                "channel_set, probe_ssid_set, probe_rate_mean, companion_macs, "
                "presence_pct_30d, total_observations) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    profile["mac"],
                    profile["updated_at"],
                    profile["time_histogram"],
                    profile["rssi_mean"],
                    profile["rssi_stddev"],
                    profile["rssi_p95"],
                    profile["channel_set"],
                    profile["probe_ssid_set"],
                    profile["probe_rate_mean"],
                    profile["companion_macs"],
                    profile["presence_pct_30d"],
                    profile["total_observations"],
                ),
            )
            stats["profiles_updated"] += 1

        # Run probe-set clustering
        clusters = compute_probe_clusters(
            conn,
            jaccard_threshold=cfg.detection.probe_cluster_jaccard,
        )
        stats["clusters_found"] = len(clusters)

        for cluster in clusters:
            writer.execute(
                "INSERT OR REPLACE INTO probe_clusters "
                "(cluster_id, created_at, updated_at, ssid_set, device_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    cluster["cluster_id"],
                    cluster["created_at"],
                    cluster["updated_at"],
                    cluster["ssid_set"],
                    cluster["device_count"],
                ),
            )
            for member in cluster["members"]:
                writer.execute(
                    "INSERT OR REPLACE INTO probe_cluster_members "
                    "(cluster_id, mac, joined_at, jaccard_score) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        member["cluster_id"],
                        member["mac"],
                        member["joined_at"],
                        member["jaccard_score"],
                    ),
                )

            # Update devices with probe_cluster_id
            for member in cluster["members"]:
                writer.execute(
                    "UPDATE devices SET probe_cluster_id = ? WHERE mac = ?",
                    (cluster["cluster_id"], member["mac"]),
                )

        # --- Stage 14b: IE-fingerprint clustering ---
        # Runs after SSID-Jaccard clustering and uses the same read-only
        # conn for the analysis pass. Persistence is done via a short-lived
        # direct connection with BEGIN IMMEDIATE so DELETE+INSERT of the
        # rebuild block is atomic (the writer thread commits per statement
        # and can't express that).
        ie_clusters = compute_ie_clusters(conn)
        stats["ie_clusters_found"] = len(ie_clusters)
        stats["ie_cluster_members"] = sum(
            len(c["members"]) for c in ie_clusters
        )
        # Always call persist: an empty list still correctly wipes stale
        # IE rows from prior runs (rebuild-from-scratch semantics).
        _persist_ie_clusters(db_path, ie_clusters)

        # --- Stage 14d: BLE manufacturer-data clustering ---
        # Independent evidence path, same rebuild-from-scratch semantics
        # as IE clustering. Empty result still wipes stale BLE rows.
        ble_clusters = compute_ble_clusters(conn)
        stats["ble_clusters_found"] = len(ble_clusters)
        stats["ble_cluster_members"] = sum(
            len(c["members"]) for c in ble_clusters
        )
        _persist_ble_clusters(db_path, ble_clusters)

        # --- Stage 14e: Service-UUID clustering ---
        service_uuid_clusters = compute_service_uuid_clusters(conn)
        stats["service_uuid_clusters_found"] = len(service_uuid_clusters)
        stats["service_uuid_cluster_members"] = sum(
            len(c["members"]) for c in service_uuid_clusters
        )
        _persist_service_uuid_clusters(db_path, service_uuid_clusters)

        # --- Stage 14f: Cross-modality copresence clustering ---
        copresence_clusters = compute_copresence_clusters(conn)
        stats["copresence_clusters_found"] = len(copresence_clusters)
        stats["copresence_cluster_members"] = sum(
            len(c["members"]) for c in copresence_clusters
        )
        _persist_copresence_clusters(db_path, copresence_clusters)

        # --- Stage 17a: Cross-modality identification aggregation ---
        # Non-fatal: identification work runs after all clustering and
        # is wrapped in its own try/except. A failure here MUST NOT
        # poison clustering output or future profiler ticks.
        try:
            from sentinel.identification import run_incremental
            from sentinel.identity.loader import load_identity_map

            identities_dir = cfg.resolved_db_path.parent / "identities"
            identity_map = (
                load_identity_map(identities_dir)
                if identities_dir.exists()
                else {}
            )
            ident_counts = run_incremental(db_path, identity_map)
            logger.info("Stage 17a aggregation: %s", ident_counts)
        except Exception:
            logger.exception(
                "Stage 17a aggregation failed (non-fatal, "
                "clustering output unaffected)"
            )

        logger.info(
            "Profiler complete: %d profiles updated, %d skipped, "
            "%d ssid clusters, %d ie clusters (%d members), "
            "%d ble clusters (%d members), "
            "%d service_uuid clusters (%d members), "
            "%d copresence clusters (%d members)",
            stats["profiles_updated"],
            stats["profiles_skipped"],
            stats["clusters_found"],
            stats["ie_clusters_found"],
            stats["ie_cluster_members"],
            stats["ble_clusters_found"],
            stats["ble_cluster_members"],
            stats["service_uuid_clusters_found"],
            stats["service_uuid_cluster_members"],
            stats["copresence_clusters_found"],
            stats["copresence_cluster_members"],
        )

    finally:
        conn.close()
        if own_writer:
            writer.stop()

    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the profiler from the command line."""
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel profiler engine")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    load_config(args.config)

    from sentinel.common.logging import setup_logging
    setup_logging("profiler")

    stats = run_profiler()
    print(f"Profiles updated:    {stats['profiles_updated']}")
    print(f"Profiles skipped:    {stats['profiles_skipped']}")
    print(f"SSID clusters:       {stats['clusters_found']}")
    print(f"IE clusters:         {stats['ie_clusters_found']}")
    print(f"IE cluster members:  {stats['ie_cluster_members']}")
    print(f"BLE clusters:        {stats['ble_clusters_found']}")
    print(f"BLE cluster members: {stats['ble_cluster_members']}")


if __name__ == "__main__":
    main()
