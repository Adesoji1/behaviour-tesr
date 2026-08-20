"""
Open Location Code (Google "Plus Codes") — LOCAL, OFFLINE, deterministic decoder.

Implements the open OLC specification (https://github.com/google/open-location-code, Apache-2.0
ALGORITHM — this is a clean-room implementation from the public spec, no third-party code, no
dependency, no external service, no API, no LLM). Decodes full codes directly and recovers short
codes (e.g. "GGVW+VH") against a reference locality centroid.

Public helpers (all fail-safe — return None / False, never raise):
  is_valid(code) / is_full(code) / is_short(code)
  encode(lat, lon, code_length=10) -> code
  decode(code) -> CodeArea(lat_lo, lon_lo, lat_hi, lon_hi, lat_center, lon_center, code_length)
  recover_nearest(short_code, ref_lat, ref_lon) -> full_code
  extract(text) -> the Plus Code token found in an address string, or None
  resolve(text, ref_latlon=None) -> (lat, lon, precision_m) or None
"""
from __future__ import annotations

import math
import re

_ALPHABET = "23456789CFGHJMPQRVWX"
_BASE = 20
_SEP = "+"
_SEP_POS = 8
_PAD = "0"
_LAT_MAX = 90
_LON_MAX = 180
_PAIR_LEN = 10
_MAX_DIGITS = 15
_GRID_COLS = 4
_GRID_ROWS = 5
_PAIR_PRECISION = _BASE ** 3                                  # 8000
_PAIR_FIRST_PV = _BASE ** (_PAIR_LEN // 2 - 1)                # 20^4 = 160000
_FINAL_LAT_PRECISION = _PAIR_PRECISION * _GRID_ROWS ** (_MAX_DIGITS - _PAIR_LEN)   # 8000 * 5^5
_FINAL_LON_PRECISION = _PAIR_PRECISION * _GRID_COLS ** (_MAX_DIGITS - _PAIR_LEN)   # 8000 * 4^5
_GRID_LAT_FIRST_PV = _GRID_ROWS ** (_MAX_DIGITS - _PAIR_LEN - 1)   # 5^4
_GRID_LON_FIRST_PV = _GRID_COLS ** (_MAX_DIGITS - _PAIR_LEN - 1)   # 4^4

# a Plus Code token inside free text: 2-8 code chars, "+", 0-7 more, optional trailing separators
_TOKEN_RE = re.compile(r"\b([" + _ALPHABET + _PAD + r"]{2,8}\+[" + _ALPHABET + r"]{0,7})\b", re.I)


class CodeArea:
    def __init__(self, lat_lo, lon_lo, lat_hi, lon_hi, code_length):
        self.lat_lo, self.lon_lo, self.lat_hi, self.lon_hi = lat_lo, lon_lo, lat_hi, lon_hi
        self.code_length = code_length
        self.lat_center = min(_LAT_MAX, max(-_LAT_MAX, (lat_lo + lat_hi) / 2.0))
        self.lon_center = min(_LON_MAX, max(-_LON_MAX, (lon_lo + lon_hi) / 2.0))


def _clean(code: str) -> str:
    return (code or "").upper()


def is_valid(code) -> bool:
    if not code or not isinstance(code, str) or _SEP not in code or code.count(_SEP) != 1:
        return False
    c = code.upper()
    if c.index(_SEP) > _SEP_POS or c.index(_SEP) % 2 == 1:
        return False
    # padding: only before the separator, contiguous, even count, and separator right after padding
    if _PAD in c:
        if c.index(_SEP) < _SEP_POS:                          # padded codes must have '+' at pos 8
            return False
        if c.find(_PAD) == -1 or c.rstrip(_PAD)[-1:] == "":
            return False
        pad_section = c[:c.index(_SEP)]
        if _PAD in pad_section.strip(_PAD):                   # padding must be contiguous & trailing
            return False
        if len(pad_section.replace(_PAD, "")) % 2 == 1:
            return False
        if c[c.index(_SEP) + 1:]:                             # nothing after '+' for a padded code
            return False
    after = c[c.index(_SEP) + 1:]
    if len(after) == 1:
        return False                                          # exactly one char after '+' is invalid
    for ch in c.replace(_SEP, "").replace(_PAD, ""):
        if ch not in _ALPHABET:
            return False
    return True


def is_short(code) -> bool:
    if not is_valid(code):
        return False
    return code.upper().index(_SEP) < _SEP_POS


def is_full(code) -> bool:
    return is_valid(code) and not is_short(code)


def encode(latitude, longitude, code_length: int = _PAIR_LEN) -> str | None:
    """Encode lat/lon to a full Plus Code of the given length (>=2, even). None on bad input."""
    try:
        lat = float(latitude); lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if code_length < 2 or (code_length < _PAIR_LEN and code_length % 2 == 1):
        return None
    code_length = min(code_length, _MAX_DIGITS)
    lat = min(_LAT_MAX - 1e-9, max(-_LAT_MAX, lat))
    lon = ((lon - -_LON_MAX) % (2 * _LON_MAX)) + -_LON_MAX     # normalise longitude to [-180,180)
    lat_val = int(round((lat + _LAT_MAX) * _FINAL_LAT_PRECISION))
    lon_val = int(round((lon + _LON_MAX) * _FINAL_LON_PRECISION))
    code = [""] * _MAX_DIGITS
    # grid digits (positions 10..14)
    if code_length > _PAIR_LEN:
        for i in range(_MAX_DIGITS - _PAIR_LEN):
            r = lat_val % _GRID_ROWS
            c = lon_val % _GRID_COLS
            code[_MAX_DIGITS - 1 - i] = _ALPHABET[r * _GRID_COLS + c]
            lat_val //= _GRID_ROWS
            lon_val //= _GRID_COLS
    else:
        lat_val //= _GRID_ROWS ** (_MAX_DIGITS - _PAIR_LEN)
        lon_val //= _GRID_COLS ** (_MAX_DIGITS - _PAIR_LEN)
    # pair digits (positions 0..9)
    for i in range(_PAIR_LEN // 2):
        code[9 - 2 * i] = _ALPHABET[lon_val % _BASE]
        code[8 - 2 * i] = _ALPHABET[lat_val % _BASE]
        lat_val //= _BASE
        lon_val //= _BASE
    s = "".join(code)
    if code_length < _SEP_POS:                                # pad short lengths
        s = s[:code_length] + _PAD * (_SEP_POS - code_length)
        return s[:_SEP_POS] + _SEP
    return s[:_SEP_POS] + _SEP + s[_SEP_POS:code_length]


def decode(code):
    """Decode a FULL Plus Code to a CodeArea. None if invalid or short. Never raises."""
    try:
        if not is_full(code):
            return None
        c = _clean(code).replace(_SEP, "")
        c = c.rstrip(_PAD)                                     # strip trailing padding
        # pair section
        lat = -_LAT_MAX * _PAIR_PRECISION
        lon = -_LON_MAX * _PAIR_PRECISION
        pv = _PAIR_FIRST_PV
        digits = min(len(c), _PAIR_LEN)
        for i in range(0, digits, 2):
            lat += _ALPHABET.index(c[i]) * pv
            lon += _ALPHABET.index(c[i + 1]) * pv
            if i < digits - 2:
                pv //= _BASE
        lat_res = pv / _PAIR_PRECISION
        lon_res = pv / _PAIR_PRECISION
        lat_lo = lat / _PAIR_PRECISION
        lon_lo = lon / _PAIR_PRECISION
        # grid section
        if len(c) > _PAIR_LEN:
            glat = 0; glon = 0
            rpv = _GRID_LAT_FIRST_PV; cpv = _GRID_LON_FIRST_PV
            gdigits = min(len(c), _MAX_DIGITS)
            for i in range(_PAIR_LEN, gdigits):
                v = _ALPHABET.index(c[i])
                glat += (v // _GRID_COLS) * rpv
                glon += (v % _GRID_COLS) * cpv
                if i < gdigits - 1:
                    rpv //= _GRID_ROWS
                    cpv //= _GRID_COLS
            lat_res = rpv / _FINAL_LAT_PRECISION
            lon_res = cpv / _FINAL_LON_PRECISION
            lat_lo = lat / _PAIR_PRECISION + glat / _FINAL_LAT_PRECISION
            lon_lo = lon / _PAIR_PRECISION + glon / _FINAL_LON_PRECISION
        return CodeArea(lat_lo, lon_lo, lat_lo + lat_res, lon_lo + lon_res, min(len(c), _MAX_DIGITS))
    except Exception:
        return None


def recover_nearest(short_code, ref_lat, ref_lon):
    """Expand a SHORT code (e.g. 'GGVW+VH') to the nearest FULL code around (ref_lat, ref_lon). Never
    raises; returns None if the short code is invalid or the reference is unusable."""
    try:
        if not is_short(short_code):
            return None if not is_full(short_code) else short_code.upper()
        rlat = min(_LAT_MAX, max(-_LAT_MAX, float(ref_lat)))
        rlon = float(ref_lon)
        sc = short_code.upper()
        pad = _SEP_POS - sc.index(_SEP)                       # missing leading digits (e.g. 4)
        resolution = _BASE ** (2 - (pad / 2.0))
        half = resolution / 2.0
        ref_code = encode(rlat, rlon, _PAIR_LEN)
        if not ref_code:
            return None
        full = ref_code[:pad] + sc
        area = decode(full)
        if area is None:
            return None
        clat, clon = area.lat_center, area.lon_center
        if rlat + half < clat and clat - resolution >= -_LAT_MAX:
            clat -= resolution
        elif rlat - half > clat and clat + resolution <= _LAT_MAX:
            clat += resolution
        if rlon + half < clon:
            clon -= resolution
        elif rlon - half > clon:
            clon += resolution
        return encode(clat, clon, len(full.replace(_SEP, "").rstrip(_PAD)))
    except Exception:
        return None


def extract(text):
    """Find the first valid-looking Plus Code token in an address string, or None."""
    if not text or not isinstance(text, str):
        return None
    m = _TOKEN_RE.search(text.upper())
    if not m:
        return None
    tok = m.group(1)
    return tok if is_valid(tok) else None


def _precision_m(area: CodeArea) -> float:
    """Approximate ground precision (metres) of a decoded area (the larger side of its box)."""
    lat_m = (area.lat_hi - area.lat_lo) * 111_320.0
    lon_m = (area.lon_hi - area.lon_lo) * 111_320.0 * max(0.1, math.cos(math.radians(area.lat_center)))
    return max(lat_m, lon_m)


def resolve(text, ref_latlon=None):
    """Extract + decode a Plus Code from `text`. Full codes decode standalone; short codes need
    ref_latlon (the locality centroid). Returns (lat, lon, precision_m) or None. Never raises."""
    try:
        tok = extract(text)
        if tok is None:
            return None
        if is_full(tok):
            area = decode(tok)
        elif is_short(tok):
            if not ref_latlon:
                return None                                   # cannot expand a short code without a reference
            full = recover_nearest(tok, ref_latlon[0], ref_latlon[1])
            area = decode(full) if full else None
        else:
            return None
        if area is None:
            return None
        return (area.lat_center, area.lon_center, _precision_m(area))
    except Exception:
        return None
