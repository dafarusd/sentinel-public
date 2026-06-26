"""rtl_433 protocol → (target_table, category) routing.

rtl_433 emits one JSON object per decoded transmission. The ``model``
field is a short human string (e.g. ``"Schrader-EG53MA4"``,
``"Acurite-Tower"``, ``"Honeywell-ActivLink"``) that we substring-match
against this table to pick a destination table and an optional category
hint.

First match wins. Anything unmatched falls through to
``("sdr_ism", "unknown")`` — never dropped — so we can refine the map
later without losing data already captured.

Kept as a flat list (not a dict) because dict iteration order in Python
3.7+ preserves insertion order but the *intent* of "first match wins" is
clearer with a list of tuples.
"""

from __future__ import annotations

# Order matters: TPMS first (most-specific manufacturer prefixes), then
# weather, then ISM-with-known-category. The unknown fallback is applied
# by match_protocol() rather than living in the table.
PROTOCOL_MAP: list[tuple[str, tuple[str, str | None]]] = [
    # --- TPMS ---
    # rtl_433's TPMS decoders almost always include "TPMS" in the model
    # name, but a handful are named after the OEM only. Both shapes are
    # listed so a Schrader-named decoder routes to TPMS even if the
    # generic "TPMS" rule wouldn't match.
    ("TPMS",        ("sdr_tpms", None)),
    ("Schrader",    ("sdr_tpms", None)),
    ("Toyota",      ("sdr_tpms", None)),
    ("Ford",        ("sdr_tpms", None)),
    ("Renault",     ("sdr_tpms", None)),
    ("Citroen",     ("sdr_tpms", None)),
    ("Hyundai",     ("sdr_tpms", None)),

    # --- Weather ---
    ("Acurite",         ("sdr_weather", None)),
    ("LaCrosse",        ("sdr_weather", None)),
    ("AmbientWeather",  ("sdr_weather", None)),
    ("Bresser",         ("sdr_weather", None)),
    ("Fineoffset",      ("sdr_weather", None)),
    ("Oregon",          ("sdr_weather", None)),
    ("Nexus",           ("sdr_weather", None)),
    ("Rubicson",        ("sdr_weather", None)),

    # --- ISM with explicit category guesses ---
    ("Honeywell",       ("sdr_ism", "alarm")),
    ("Linear",          ("sdr_ism", "garage")),
    ("Genie",           ("sdr_ism", "garage")),
    ("Chamberlain",     ("sdr_ism", "garage")),
    ("Doorbell",        ("sdr_ism", "doorbell")),
    ("Petrainer",       ("sdr_ism", "pet")),
]

_UNKNOWN: tuple[str, str] = ("sdr_ism", "unknown")


def match_protocol(model: str | None) -> tuple[str, str | None]:
    """Map an rtl_433 ``model`` string to (target_table, category).

    Case-sensitive substring match, first hit wins. Empty/None ``model``
    returns the unknown fallback so the caller never has to special-case.
    """
    if not model:
        return _UNKNOWN
    for needle, route in PROTOCOL_MAP:
        if needle in model:
            return route
    return _UNKNOWN
