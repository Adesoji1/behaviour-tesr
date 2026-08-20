"""
Local, deterministic location -> coordinate registry (phase2).

Resolves the TRANSACTION's supplied location string (additional_info.location) to approximate
coordinates using a FIRST-PARTY, curated dataset (ml/data/ng_locations.json) — NO external API, NO
licensed dataset, NO LLM-invented coordinates. City/state-level granularity only.

Confident-match policy (unknown/ambiguous stay UNRESOLVED):
  * Placeholders ("", "-", ".", "N/A", …) -> unresolved.
  * Nigerian addresses are structured "…, City <postcode>, State, Nigeria". We drop the trailing
    country, then match the LAST segment against STATES and the second-to-last against CITIES (city
    preferred, more specific). Segments are cleaned to alphabetic tokens (postcodes / Plus Codes /
    house numbers dropped), and matched by EXACT equality to a registry key — so street names do not
    accidentally match a state. No fuzzy/substring matching, no guessing.

Never raises. Loaded once at import; a missing/broken dataset yields an empty registry (lookup=None).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("ml.geo_registry")

_DATA = Path(__file__).resolve().parent / "data" / "ng_locations.json"
_PLACEHOLDERS = {"", "-", ".", "..", "...", "n/a", "n\\a", "na", "null", "none", "nil", "unknown", "nan"}
_COUNTRY_TOKENS = {"nigeria", "ng", "nga"}

_STATES: dict[str, list] = {}
_CITIES: dict[str, list] = {}


def _load() -> None:
    global _STATES, _CITIES
    try:
        blob = json.loads(_DATA.read_text())
        _STATES = {k.lower().strip(): v for k, v in (blob.get("states") or {}).items()}
        _CITIES = {k.lower().strip(): v for k, v in (blob.get("cities") or {}).items()}
        log.info("geo_registry: loaded %d states + %d cities", len(_STATES), len(_CITIES))
    except Exception as e:
        _STATES, _CITIES = {}, {}
        log.warning("geo_registry: could not load %s (%s) — location resolution disabled", _DATA, e)


_load()


def is_placeholder(location) -> bool:
    """True for missing / placeholder location strings that carry no geographic evidence."""
    if not location or not isinstance(location, str):
        return True
    return location.strip().lower() in _PLACEHOLDERS


def _clean_seg(seg: str) -> str:
    """Keep only alphabetic tokens of a comma-segment (drop postcodes, Plus Codes, house numbers),
    so 'Lafia 950101' -> 'lafia', 'GGVW+VH' -> '', 'Benin City' -> 'benin city'."""
    out = []
    for tok in seg.lower().replace("/", " ").split():
        t = tok.strip(".")
        if t.isalpha() or ("-" in t and t.replace("-", "").isalpha()):
            out.append(t)
    return " ".join(out).strip()


def lookup(location):
    """Resolve a location string to (lat, lon, matched_name, granularity) or None. Confident matches
    only; placeholders / unknown / ambiguous -> None. Never raises."""
    if is_placeholder(location) or (not _STATES and not _CITIES):
        return None
    try:
        segs = [s.strip() for s in str(location).split(",") if s.strip()]
        cleaned = [_clean_seg(s) for s in segs]
        cleaned = [c for c in cleaned if c and c not in _COUNTRY_TOKENS]
        if not cleaned:
            return None
        state_cand = cleaned[-1]
        city_cand = cleaned[-2] if len(cleaned) >= 2 else cleaned[-1]
        # city is more specific -> prefer it
        if city_cand in _CITIES:
            la, lo = _CITIES[city_cand]
            return (float(la), float(lo), city_cand, "city")
        if state_cand in _CITIES:                  # single-segment inputs like "Lagos"
            la, lo = _CITIES[state_cand]
            return (float(la), float(lo), state_cand, "city")
        if state_cand in _STATES:
            la, lo = _STATES[state_cand]
            return (float(la), float(lo), state_cand, "state")
        if city_cand in _STATES:
            la, lo = _STATES[city_cand]
            return (float(la), float(lo), city_cand, "state")
        return None
    except Exception as e:
        log.debug("geo_registry: lookup failed for %r (%s)", location, e)
        return None
