"""Canonical 802.11 Information Element fingerprint hasher.

Given the raw IE byte stream captured from a probe request, produce a stable
SHA-256 hash of the structural fingerprint — the subset of IEs that reflect
hardware/driver/OS capabilities and survive MAC randomization.

Included IEs (structural, stable across MAC rotation):
    ID   1  — Supported Rates                 (sorted bytes)
    ID  45  — HT Capabilities                  (raw info)
    ID  50  — Extended Supported Rates         (sorted bytes)
    ID 127  — Extended Capabilities            (raw info)
    ID 191  — VHT Capabilities                 (raw info)
    ID 221  — Vendor-specific                  (first 3 bytes: OUI only)
    ID 255  — Element ID Extension             (ext sub-ID + raw info)

Excluded IEs (dynamic or query-key, not fingerprint material):
    ID   0  — SSID                             (query key, not fingerprint)
    ID   3  — DS Parameter Set                 (channel, environmental)
    ID  48  — RSN                              (network security context)
    everything else — quietly dropped

The canonical representation is deterministic: IE records are sorted by
(id, ext_id) and each record is length-prefixed so `0x01 0xab` and
`0x01 0x01 0xab 0x00` cannot collide. SHA-256 over that byte string, hex.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

# IE IDs we hash.
_ID_SUPPORTED_RATES = 1
_ID_HT_CAPS = 45
_ID_EXT_SUPPORTED_RATES = 50
_ID_EXT_CAPS = 127
_ID_VHT_CAPS = 191
_ID_VENDOR = 221
_ID_EXTENSION = 255

_INCLUDED_IDS = frozenset({
    _ID_SUPPORTED_RATES,
    _ID_HT_CAPS,
    _ID_EXT_SUPPORTED_RATES,
    _ID_EXT_CAPS,
    _ID_VHT_CAPS,
    _ID_VENDOR,
    _ID_EXTENSION,
})

# IDs whose info bytes should be sorted before hashing (order-insensitive fields).
_SORTED_IDS = frozenset({_ID_SUPPORTED_RATES, _ID_EXT_SUPPORTED_RATES})

# Vendor-specific IEs: keep only the 3-byte OUI prefix. Payloads after the OUI
# routinely contain volatile state (beacon timestamps, association counters,
# per-AP negotiation bits) that would destabilize the fingerprint.
_VENDOR_OUI_LEN = 3


def parse_ies(ie_bytes: bytes) -> list[tuple[int, int | None, bytes]]:
    """Walk a raw 802.11 IE TLV stream.

    Returns a list of (element_id, ext_id, info) tuples. ``ext_id`` is the
    1-byte sub-identifier that follows the length byte for ID 255 elements;
    None for all other IDs.

    Tolerant of truncation: if the stream ends mid-element, parsing stops
    cleanly and returns what was successfully parsed so far.
    """
    result: list[tuple[int, int | None, bytes]] = []
    n = len(ie_bytes)
    i = 0
    while i + 2 <= n:
        element_id = ie_bytes[i]
        length = ie_bytes[i + 1]
        body_start = i + 2
        body_end = body_start + length
        if body_end > n:
            break  # truncated — stop here, keep what we have
        info = ie_bytes[body_start:body_end]

        ext_id: int | None = None
        if element_id == _ID_EXTENSION and len(info) >= 1:
            ext_id = info[0]
            info = info[1:]

        result.append((element_id, ext_id, info))
        i = body_end
    return result


def _encode_record(element_id: int, ext_id: int | None, info: bytes) -> bytes:
    """Length-prefix a single canonical IE record.

    Format:  id(1) | ext_id_present(1) | [ext_id(1)] | len(2 BE) | info

    The ext_id-present flag plus explicit length prefix guarantee that
    different (id, ext_id, info) triples never produce colliding encodings
    under concatenation.
    """
    parts = bytearray()
    parts.append(element_id & 0xFF)
    if ext_id is None:
        parts.append(0)
    else:
        parts.append(1)
        parts.append(ext_id & 0xFF)
    parts.append((len(info) >> 8) & 0xFF)
    parts.append(len(info) & 0xFF)
    parts.extend(info)
    return bytes(parts)


def _canonicalize_record(
    element_id: int, ext_id: int | None, info: bytes
) -> tuple[int, int | None, bytes] | None:
    """Reduce one parsed IE to its canonical contribution, or None to skip."""
    if element_id not in _INCLUDED_IDS:
        return None

    if element_id == _ID_VENDOR:
        if len(info) < _VENDOR_OUI_LEN:
            return None  # malformed vendor IE — can't trust it
        info = info[:_VENDOR_OUI_LEN]
    elif element_id in _SORTED_IDS:
        info = bytes(sorted(info))

    return (element_id, ext_id, info)


def canonical_ie_representation(ie_bytes: bytes | None) -> bytes | None:
    """Build the canonical byte string that is hashed.

    Returns None if ``ie_bytes`` is None/empty or if no fingerprintable IEs
    are present after filtering. Callers should treat None as "no fingerprint
    available" and store NULL rather than inventing a placeholder hash.
    """
    if not ie_bytes:
        return None

    records: list[tuple[int, int | None, bytes]] = []
    for element_id, ext_id, info in parse_ies(ie_bytes):
        reduced = _canonicalize_record(element_id, ext_id, info)
        if reduced is not None:
            records.append(reduced)

    if not records:
        return None

    # Sort by (id, ext_id with None < any int, info). Multiple vendor IEs in
    # one probe (distinct OUIs) are legitimate; sort keeps order stable.
    records.sort(key=lambda r: (r[0], -1 if r[1] is None else r[1], r[2]))

    return b"".join(_encode_record(eid, ext, body) for eid, ext, body in records)


def ie_fingerprint_hash(ie_bytes: bytes | None) -> str | None:
    """SHA-256 hex of the canonical IE fingerprint, or None.

    None is returned when the probe carries no fingerprintable structure
    (malformed, empty, or only contains excluded IEs like SSID-only probes).
    """
    canonical = canonical_ie_representation(ie_bytes)
    if canonical is None:
        return None
    return hashlib.sha256(canonical).hexdigest()


def included_ie_ids() -> Iterable[int]:
    """Expose the included-ID set for diagnostics/tooling."""
    return frozenset(_INCLUDED_IDS)
