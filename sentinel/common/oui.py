"""OUI (Organizationally Unique Identifier) vendor lookup.

Loads the IEEE oui.txt file and resolves MAC prefixes to vendor names.
The file is loaded once at first lookup and cached in memory (~3MB dict).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sentinel.config import get_config

logger = logging.getLogger("sentinel.oui")

# Module-level cache
_oui_db: dict[str, str] | None = None


def _load_oui(oui_path: Path) -> dict[str, str]:
    """Parse the IEEE oui.txt file into a prefix->vendor dict.

    Handles the standard IEEE format:
        XX-XX-XX   (hex)		Vendor Name

    Returns:
        Dict mapping uppercase MAC prefix (e.g. "AA:BB:CC") to vendor name.
    """
    db: dict[str, str] = {}

    if not oui_path.exists():
        logger.warning("OUI file not found at %s — vendor lookups disabled", oui_path)
        return db

    # Match lines like: "00-00-00   (hex)\t\tXerox Corporation"
    pattern = re.compile(r"^([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})\s+\(hex\)\s+(.+)$")

    with open(oui_path, errors="replace") as f:
        for line in f:
            m = pattern.match(line.strip())
            if m:
                prefix = f"{m.group(1)}:{m.group(2)}:{m.group(3)}".upper()
                vendor = m.group(4).strip()
                db[prefix] = vendor

    logger.info("Loaded %d OUI entries from %s", len(db), oui_path)
    return db


def lookup_vendor(mac: str) -> str | None:
    """Look up the vendor for a MAC address.

    Args:
        mac: MAC address in any common format (aa:bb:cc:dd:ee:ff,
             AA-BB-CC-DD-EE-FF, aabbccddeeff).

    Returns:
        Vendor name string, or None if not found.
    """
    global _oui_db

    if _oui_db is None:
        cfg = get_config()
        _oui_db = _load_oui(cfg.resolved_oui_path)

    # Normalize MAC to uppercase colon-separated
    clean = mac.upper().replace("-", ":").replace(".", "")
    if ":" not in clean and len(clean) == 12:
        clean = ":".join(clean[i:i + 2] for i in range(0, 12, 2))

    prefix = clean[:8]  # "AA:BB:CC"
    return _oui_db.get(prefix)


def is_locally_administered(mac: str) -> bool:
    """Check if a MAC address is locally administered (randomized).

    The second least significant bit of the first octet is set for
    locally administered addresses (bit 1 of first byte).

    Args:
        mac: MAC address in any common format.

    Returns:
        True if the MAC is locally administered (likely randomized).
    """
    clean = mac.upper().replace("-", ":").replace(".", "")
    if ":" not in clean and len(clean) >= 2:
        first_octet = int(clean[:2], 16)
    else:
        first_octet = int(clean.split(":")[0], 16)

    return bool(first_octet & 0x02)
