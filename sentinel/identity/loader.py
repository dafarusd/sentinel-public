"""Identity dossier loader.

Reads YAML files from the identities/ directory and produces a flat map
of identifier (MAC or sensor_id) -> identity_id for ingest-time tagging.

Supported YAML shapes:
  - user.yaml: identity.id at top, devices[].macs.{wifi,bt}[].mac,
    household_context.* (router, modem, cameras_*, consoles, etc),
    each entry having a .mac or being a {mac, name} dict.
  - unknowns/*.yaml: identity.id at top, device.mac (single string)
    or device.macs (list), or device.sensor_ids (TPMS), or
    device.device_id (sdr_ism).

The loader is intentionally permissive: malformed or unrecognized YAML
is logged and skipped, never raised. Goal is to never break ingest.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _normalize_mac(value: str) -> str:
    """Lowercase, strip whitespace. Returns empty string for non-string input."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _extract_identifiers_from_user(doc: dict[str, Any]) -> list[str]:
    """Pull all MACs from a top-level user-style YAML."""
    ids: list[str] = []

    # devices[].macs.{wifi,bt}[].mac
    for dev in doc.get("devices", []) or []:
        macs = dev.get("macs") or {}
        for source in ("wifi", "bt"):
            for entry in macs.get(source) or []:
                mac = entry.get("mac") if isinstance(entry, dict) else None
                if mac:
                    ids.append(_normalize_mac(mac))
        # SDR sensor IDs (TPMS)
        for sid in dev.get("sensor_ids") or []:
            if sid:
                ids.append(_normalize_mac(str(sid)))

    # household_context.*  — accept three shapes:
    #   list of strings (cameras_wyze: [a, b])
    #   list of {mac, name} dicts (televisions: [{mac, name}])
    #   list of {mac} dicts (router: [{mac}])
    hc = doc.get("household_context") or {}
    for category, entries in hc.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                ids.append(_normalize_mac(entry))
            elif isinstance(entry, dict):
                mac = entry.get("mac")
                if mac:
                    ids.append(_normalize_mac(mac))

    return [i for i in ids if i]


def _extract_identifiers_from_unknown(doc: dict[str, Any]) -> list[str]:
    """Pull MACs/sensor_ids from an unknowns/*.yaml shape."""
    ids: list[str] = []
    device = doc.get("device") or {}

    # Single mac
    mac = device.get("mac")
    if mac:
        ids.append(_normalize_mac(mac))

    # List of macs
    for m in device.get("macs") or []:
        if isinstance(m, str):
            ids.append(_normalize_mac(m))
        elif isinstance(m, dict) and m.get("mac"):
            ids.append(_normalize_mac(m["mac"]))

    # Sensor IDs (TPMS, ISM)
    for sid in device.get("sensor_ids") or []:
        if sid:
            ids.append(_normalize_mac(str(sid)))

    # Single device_id (sdr_ism)
    did = device.get("device_id")
    if did:
        ids.append(_normalize_mac(str(did)))

    # Station ID (sdr_weather)
    sid = device.get("station_id")
    if sid:
        ids.append(_normalize_mac(str(sid)))

    return [i for i in ids if i]


def load_identity_map(identities_dir: Path) -> dict[str, str]:
    """Walk identities directory, return identifier -> identity_id mapping.

    Args:
        identities_dir: Root directory (e.g., /mnt/ssd/sentinel-data/identities)

    Returns:
        Dict mapping each identifier (MAC, sensor_id, etc) to its identity_id.
        Last-loaded YAML wins on collision (logged as warning).
    """
    if not identities_dir.exists():
        logger.warning("Identities dir not found: %s", identities_dir)
        return {}

    identifier_map: dict[str, str] = {}
    files_loaded = 0

    for yaml_path in sorted(identities_dir.rglob("*.yaml")):
        try:
            doc = yaml.safe_load(yaml_path.read_text())
        except Exception as exc:
            logger.error("Failed to parse %s: %s", yaml_path, exc)
            continue

        if not isinstance(doc, dict):
            logger.warning("Skipping %s: not a YAML dict", yaml_path)
            continue

        identity_id = (doc.get("identity") or {}).get("id")
        if not identity_id:
            logger.warning("Skipping %s: no identity.id", yaml_path)
            continue

        # Try both shapes — most YAMLs match exactly one
        ids = _extract_identifiers_from_user(doc)
        ids.extend(_extract_identifiers_from_unknown(doc))

        for ident in set(ids):
            if ident in identifier_map and identifier_map[ident] != identity_id:
                logger.warning(
                    "Identifier %s already maps to %s, overwriting with %s (file: %s)",
                    ident, identifier_map[ident], identity_id, yaml_path,
                )
            identifier_map[ident] = identity_id

        files_loaded += 1

    logger.info(
        "Loaded %d identifiers from %d YAML files in %s",
        len(identifier_map), files_loaded, identities_dir,
    )
    return identifier_map


def lookup_identity(identifier: str, identity_map: dict[str, str]) -> str | None:
    """Look up identity for a MAC or sensor ID. Returns None if unknown."""
    if not identifier:
        return None
    return identity_map.get(_normalize_mac(identifier))
