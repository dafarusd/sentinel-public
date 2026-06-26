"""Structural fingerprint hasher for BLE/classic manufacturer data.

Given the hex-encoded manufacturer_data field and the service-UUID JSON
blob captured from a Bluetooth advertisement, produce a stable SHA-256
fingerprint of the *structural* features that survive aggressive MAC
rotation (Apple devices rotate BLE MACs roughly every 15 minutes).

The hash intentionally excludes bytes that typically hold counters,
proximity nonces, or handoff state — those rotate even faster than the
MAC itself and would defeat clustering.

### Company ID byte order

Bluetooth SIG assigns each vendor a 16-bit Company ID that appears
little-endian on the wire. `bleak` and BlueZ both parse the wire bytes
into a Python int before handing the payload to the capture layer. The
capture layer then reformats as ``f"{company_id:04x}"`` which is
conventional big-endian. Empirical check against the current database
confirms leading prefixes ``004c`` (Apple), ``0075`` (Samsung),
``0006`` (Microsoft) — not ``4c00``/``7500``/``0600``. All dispatch
keys in this module are therefore big-endian.

### Per-vendor canonical slicing (v1.1b)

    004c  Apple      TLV stream, keep (type, length, value[0]) per record.
                     v1 used (type, length) only; empirical 14d validation
                     showed 6+ concurrent Apple MACs collapsing into one
                     fingerprint because the subtype *set* was shared across
                     distinct iPhone models. value[0] typically carries a
                     status/flag byte that discriminates models without
                     exposing the counter/nonce bytes at value[1:].

                     Short Apple payloads (<5 bytes) are refused before
                     TLV parsing — empirically these are dominated by
                     Nearby Info subtype 0x12 (4-byte payload), which
                     encodes user-activity state rather than device
                     identity. Clustering on them produces misleading
                     "presence" groupings that merge distinct iPhones.
                     v1.2 may narrow this exclusion to subtype 0x12
                     specifically.
    0006  Microsoft  first 2 payload bytes (scenario + type)
    0075  Samsung    first 3 payload bytes (service discriminator)
    other generic    full payload (risk: counters produce per-advert clusters,
                     acceptable for niche vendors)

Service UUIDs (when present) are sorted and appended to the hash input.
They rarely change per-device and stabilize the fingerprint further.

v2 iteration lives behind `git log` — ship this, measure distribution,
adjust if we see over- or under-clustering.
"""

from __future__ import annotations

import hashlib
import json

# Dispatch keys are big-endian hex of the 16-bit Company ID (see docstring).
_COMPANY_APPLE = "004c"
_COMPANY_MICROSOFT = "0006"
_COMPANY_SAMSUNG = "0075"

# Microsoft CDP: first byte is scenario, second is device type.
_MICROSOFT_CANON_LEN = 2

# Samsung variants differ, but the first ~3 bytes carry stable
# service/discriminator content per family.
_SAMSUNG_CANON_LEN = 3

# Minimum hex string length: 4 chars = 2 bytes = company ID only, no payload.
# Still hashable (weak single-vendor cluster), filtered at clustering time
# by the ≥2-member rule.
_MIN_MFR_HEX_LEN = 4

# Minimum payload length (bytes) for Apple manufacturer-data to be
# fingerprinted. Four-byte payloads are empirically dominated by
# Nearby Info subtype 0x12 (activity state, not identity); the next
# real subtype class in the test DB jumps to 7+ payload bytes, so 5
# cleanly separates them without excluding legitimate short-body
# identity payloads.
_APPLE_MIN_PAYLOAD_LEN = 5


def _apple_tlv_structure(payload: bytes) -> bytes:
    """Canonicalize Apple Continuity payload to its TLV structure.

    Walks the byte stream as a sequence of 1-byte-type / 1-byte-length /
    N-byte-value records and emits (type, length, value[0]) per record —
    or (type, length) if the record has zero value bytes. value[0] is a
    device-model / status discriminator on most Continuity subtypes;
    value[1:] typically carries counters, proximity nonces, or handoff
    state, which would destabilize the fingerprint, so those bytes are
    deliberately dropped.

    Tolerant of truncation: when the stream ends mid-record, whatever
    was successfully parsed is returned.
    """
    out = bytearray()
    n = len(payload)
    i = 0
    while i + 2 <= n:
        tlv_type = payload[i]
        tlv_len = payload[i + 1]
        body_end = i + 2 + tlv_len
        if body_end > n:
            break  # truncated — keep what we have
        out.append(tlv_type)
        out.append(tlv_len)
        if tlv_len >= 1:
            out.append(payload[i + 2])  # value[0] discriminator byte
        i = body_end
    return bytes(out)


def _canonicalize_payload(company_id: str, payload: bytes) -> bytes:
    """Select the per-vendor canonical slice of the payload bytes.

    Returns empty bytes to signal "refuse to fingerprint" — callers
    treat an empty canon the same as an absent one.
    """
    if company_id == _COMPANY_APPLE:
        # Refuse short payloads (< 5 bytes): empirically the 4-byte
        # Nearby Info beacon encodes user-activity state, not device
        # identity. A single-TLV payload with a long value (Handoff,
        # AirDrop, Find My) produces the same 3-byte canonical shape
        # regardless of actual value length, so gating on canon length
        # would wrongly exclude those identity-bearing messages.
        if len(payload) < _APPLE_MIN_PAYLOAD_LEN:
            return b""
        return _apple_tlv_structure(payload)
    if company_id == _COMPANY_MICROSOFT:
        return payload[:_MICROSOFT_CANON_LEN]
    if company_id == _COMPANY_SAMSUNG:
        return payload[:_SAMSUNG_CANON_LEN]
    # Niche vendors: fall through to full payload. If their payloads carry
    # counters, every advertisement hashes distinctly and the ≥2-member
    # cluster rule will simply drop them from the cluster set.
    return payload


def _canonicalize_uuids(service_uuids_json: str | None) -> bytes:
    """Parse the service-UUID JSON blob into a stable byte representation.

    Returns empty bytes on None, empty JSON, malformed JSON, or a non-list
    payload. Individual UUID strings are coerced to str before sorting to
    handle any stray non-string entries gracefully.
    """
    if not service_uuids_json:
        return b""
    try:
        uuids = json.loads(service_uuids_json)
    except (ValueError, TypeError):
        return b""
    if not isinstance(uuids, list):
        return b""
    normalized = sorted(str(u).lower() for u in uuids if u is not None)
    if not normalized:
        return b""
    return ",".join(normalized).encode("ascii", errors="replace")


def compute_mfr_fingerprint(
    mfr_hex: str | None,
    service_uuids_json: str | None,
) -> str | None:
    """SHA-256 hex of the structural BLE-advertisement fingerprint, or None.

    Args:
        mfr_hex: ``manufacturer_data_hex`` as stored in ``bt_advertisements``
            (big-endian Company ID | payload bytes, all hex-encoded). May
            be None or empty for advertisements with no manufacturer data.
        service_uuids_json: ``service_uuids`` column — a JSON array of
            UUID strings, or None.

    Returns:
        64-char hex SHA-256 digest, or None if the hex is missing, malformed,
        or shorter than the 2-byte Company ID header.
    """
    if not mfr_hex or len(mfr_hex) < _MIN_MFR_HEX_LEN:
        return None

    company_id = mfr_hex[:_MIN_MFR_HEX_LEN].lower()

    try:
        payload = bytes.fromhex(mfr_hex[_MIN_MFR_HEX_LEN:]) if len(mfr_hex) > _MIN_MFR_HEX_LEN else b""
    except ValueError:
        # Malformed hex (odd length, non-hex chars) — don't crash the
        # capture/backfill pipeline, just refuse to fingerprint.
        return None

    canon = _canonicalize_payload(company_id, payload)
    uuids = _canonicalize_uuids(service_uuids_json)

    # An empty canon means the per-vendor slicer refused this payload
    # (e.g., Apple Nearby-Info-only). If there's also no UUID content
    # then nothing fingerprintable survives — stored as NULL upstream.
    if not canon and not uuids:
        return None

    h = hashlib.sha256()
    # NUL separators between fields make concatenation collision-safe:
    # no payload byte run can span two fields without also matching a NUL.
    h.update(company_id.encode("ascii"))
    h.update(b"\x00")
    h.update(canon)
    h.update(b"\x00")
    h.update(uuids)
    return h.hexdigest()
