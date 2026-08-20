"""
Geo-velocity helpers — FIRST-PARTY, dependency-free, best-effort.

Pure, deterministic, local. NO external/paid geolocation (no MaxMind / GeoLite2 / geo APIs). Every
function is fail-safe: on any missing/invalid input it returns a NEUTRAL result (None / 0.0) and never
raises, so /score can never break or be inflated by geo. Coordinates are NEVER invented.

Waterfall (`resolve_coords`), in priority order:
  1. explicit (lat, lon) from the payload            -> source "coordinates"
  2. a FIRST-PARTY, internal IP->(lat,lon) resolver   -> source "ip"          (only if configured)
  3. a FIRST-PARTY, approved location->(lat,lon) map  -> source "location_registry" (only if configured)
  else                                                -> source "unavailable"

The IP (P2) and location (P3) resolvers are PLUGGABLE hooks (config.GEO_IP_RESOLVER /
config.GEO_LOCATION_REGISTRY, each a "module:function" path). None ship in this repo — until an
internal one is supplied, P2/P3 return None and geo stays "unavailable". Private/reserved/malformed
IPs are never resolved. The free-text location string is NEVER parsed for coordinates.
"""
from __future__ import annotations

import ipaddress
import logging
import math

from . import config

log = logging.getLogger("ml.geo")

_UNAVAILABLE = (None, None, "unavailable")
_resolver_cache: dict[str, object] = {}


def is_public_ip(ip) -> bool:
    """True only for a syntactically valid, GLOBAL (public) IP. Private / reserved / loopback /
    link-local / multicast / malformed / missing -> False (never treated as geographic evidence)."""
    if not ip or not isinstance(ip, str):
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return bool(addr.is_global) and not addr.is_multicast


def _valid_coord(lat, lon) -> bool:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if la != la or lo != lo:                       # NaN
        return False
    return -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0


def _load_hook(path: str):
    """Import a 'module:function' hook once (cached). Returns the callable or None (never raises)."""
    if not path:
        return None
    if path in _resolver_cache:
        return _resolver_cache[path]
    fn = None
    try:
        mod, _, func = path.partition(":")
        if mod and func:
            import importlib
            fn = getattr(importlib.import_module(mod), func, None)
        if not callable(fn):
            log.warning("geo: resolver hook %r is not callable — ignoring", path)
            fn = None
    except Exception as e:
        log.warning("geo: could not load resolver hook %r (%s) — ignoring", path, e)
        fn = None
    _resolver_cache[path] = fn
    return fn


def resolve_ip(ip):
    """P2: FIRST-PARTY internal IP -> (lat, lon), or None. Only a usable PUBLIC ip is attempted, and
    only when config.GEO_IP_RESOLVER points at an internal resolver. Never raises."""
    if not is_public_ip(ip):
        return None
    fn = _load_hook(config.GEO_IP_RESOLVER)
    if fn is None:
        return None
    try:
        res = fn(ip.strip())
    except Exception as e:
        log.debug("geo: ip resolver failed (%s)", e)
        return None
    if res and _valid_coord(res[0], res[1]):
        return (float(res[0]), float(res[1]))
    return None


def resolve_location(location):
    """FIRST-PARTY location-string -> (lat, lon), or None. Uses config.GEO_LOCATION_REGISTRY when
    configured (an internal override hook), else the built-in LOCAL deterministic registry
    (ml.geo_registry over ml/data/ng_locations.json). Confident matches only; the free-text string is
    NEVER heuristically parsed for coordinates here. Never raises."""
    if not location or not isinstance(location, str) or not location.strip():
        return None
    fn = _load_hook(config.GEO_LOCATION_REGISTRY)
    if fn is not None:                              # internal override hook
        try:
            res = fn(location.strip())
        except Exception as e:
            log.debug("geo: location hook failed (%s)", e)
            return None
        return (float(res[0]), float(res[1])) if res and _valid_coord(res[0], res[1]) else None
    from . import geo_registry                      # built-in local registry (default)
    hit = geo_registry.lookup(location)
    return (hit[0], hit[1]) if hit is not None else None


def resolve_plus_code(location):
    """Decode an Open Location Code (Plus Code) embedded in the location string to (lat, lon,
    precision_m). FULL codes decode standalone; SHORT codes (e.g. 'GGVW+VH') are recovered against the
    locality centroid parsed from the SAME address via the built-in registry. None if no valid,
    confidently-decodable code (or a short code with no locality reference). Never raises."""
    if not location or not isinstance(location, str):
        return None
    from . import pluscode, geo_registry
    if pluscode.extract(location) is None:
        return None
    ref = geo_registry.lookup(location)            # locality reference for short-code recovery
    ref_ll = (ref[0], ref[1]) if ref is not None else None
    return pluscode.resolve(location, ref_ll)


def resolve_coords(latitude=None, longitude=None, ip=None, location=None):
    """The waterfall (phase3 order): explicit coordinates -> Plus Code (precise) -> local city/state
    registry (coarse) -> local/pluggable IP resolver -> unavailable. Returns (lat, lon, source) with
    source in {coordinates, plus_code, location_registry, ip, unavailable}. Never invents; never raises."""
    if _valid_coord(latitude, longitude):
        return (float(latitude), float(longitude), "coordinates")
    pc = resolve_plus_code(location)               # P2: precise Plus Code (street-level)
    if pc is not None:
        return (pc[0], pc[1], "plus_code")
    hit = resolve_location(location)               # P3: coarse city/state registry
    if hit is not None:
        return (hit[0], hit[1], "location_registry")
    hit = resolve_ip(ip)                           # P4: local/pluggable GeoIP (inert unless configured)
    if hit is not None:
        return (hit[0], hit[1], "ip")
    return _UNAVAILABLE


def resolve_detail(latitude=None, longitude=None, ip=None, location=None) -> dict:
    """Full resolution detail for SHADOW telemetry — coordinates + source + granularity + the booleans
    the coverage report needs (location present/resolved, IP present/public/resolved) + a reason when
    unavailable. Never raises."""
    from . import geo_registry, pluscode
    placeholder = geo_registry.is_placeholder(location)
    pc_tok = pluscode.extract(location) if location else None
    d = {"lat": None, "lon": None, "source": "unavailable", "granularity": None, "matched": None,
         "precision_m": None, "plus_code_present": bool(pc_tok), "plus_code_decoded": False,
         "loc_present": (not placeholder), "loc_resolved": False, "loc_placeholder": placeholder,
         "ip_present": bool(ip), "ip_public": is_public_ip(ip), "ip_resolved": False, "reason": None}
    if _valid_coord(latitude, longitude):
        d.update(lat=float(latitude), lon=float(longitude), source="coordinates", granularity="exact",
                 precision_m=0.0)
        return d
    pc = resolve_plus_code(location)               # P2: precise Plus Code
    if pc is not None:
        d.update(lat=pc[0], lon=pc[1], source="plus_code", granularity="plus_code",
                 precision_m=round(pc[2], 1), plus_code_decoded=True, loc_resolved=True)
        return d
    if config.GEO_LOCATION_REGISTRY:
        hit = resolve_location(location)
        if hit is not None:
            d.update(lat=hit[0], lon=hit[1], source="location_registry", granularity="hook",
                     loc_resolved=True)
            return d
    elif d["loc_present"]:
        reg = geo_registry.lookup(location)
        if reg is not None:
            # nominal precision of a centroid: a city ~ few km, a state ~ tens of km (COARSE vs a
            # Plus Code's metres — this is the whole point of the phase3 comparison).
            prec = 8000.0 if reg[3] == "city" else 60000.0
            d.update(lat=reg[0], lon=reg[1], source="location_registry", granularity=reg[3],
                     matched=reg[2], loc_resolved=True, precision_m=prec)
            return d
    hit = resolve_ip(ip)
    if hit is not None:
        d.update(lat=hit[0], lon=hit[1], source="ip", granularity="ip", ip_resolved=True)
        return d
    if d["loc_present"] and not d["loc_resolved"]:
        d["reason"] = "unknown_location"
    elif d["ip_present"] and not d["ip_public"]:
        d["reason"] = "private_or_malformed_ip"
    else:
        d["reason"] = "no_evidence"
    return d


def haversine_km(a, b) -> float:
    """Great-circle displacement in km between (lat, lon) pairs `a` and `b`. 0.0 on bad input."""
    if not a or not b or not _valid_coord(a[0], a[1]) or not _valid_coord(b[0], b[1]):
        return 0.0
    lat1, lon1, lat2, lon2 = map(math.radians, (float(a[0]), float(a[1]), float(b[0]), float(b[1])))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2.0 * math.asin(min(1.0, math.sqrt(h)))


def geo_velocity_kmh(prev_latlon, curr_latlon, elapsed_hours) -> float:
    """RAW geo velocity in km/h: haversine(prev, curr) / elapsed_hours. Returns 0.0 whenever any input
    is missing/invalid or elapsed_hours <= 0 (never divides by zero, never negative/inf). Optionally
    clipped by config.GEO_MAX_KMH. This is the pre-squash value used for telemetry."""
    try:
        eh = float(elapsed_hours)
    except (TypeError, ValueError):
        return 0.0
    if eh <= 0 or eh != eh:
        return 0.0
    km = haversine_km(prev_latlon, curr_latlon)
    if km <= 0:
        return 0.0
    kmh = km / eh
    cap = getattr(config, "GEO_MAX_KMH", 0) or 0
    if cap and kmh > cap:
        kmh = float(cap)
    return float(kmh)


def geo_velocity_feature(prev_latlon, curr_latlon, elapsed_hours) -> float:
    """The MODEL-facing value (used only when geo is later activated as a feature): log1p(km/h),
    matching the vel_* log1p convention. 0.0 when geo-velocity is unavailable. Never raises."""
    return math.log1p(geo_velocity_kmh(prev_latlon, curr_latlon, elapsed_hours))
