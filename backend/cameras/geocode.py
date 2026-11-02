"""
Address geocoding for bulk imports.

Primary: the US Census Bureau geocoder (free, no key, no published rate
limit, understands "Decatur St & Bienville St" intersections and
range-interpolates house numbers). Fallback: Nominatim, bounded to the
RETINA BBOX, at most one request per second per their usage policy.

Both are only used by management commands, never by request handlers.
"""

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.conf import settings

log = logging.getLogger(__name__)

USER_AGENT = "retina-nola-camera-map/0.1 (+https://github.com/mwollenweber/retina)"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
CITY_SUFFIX = ", New Orleans, LA"

# Local shorthand that neither geocoder understands.
_ABBREVIATIONS = {
    r"\btchoup\b": "Tchoupitoulas St",
    r"\bst\. ": "St ",
    r"\bn\. ": "N ",
    r"\bs\. ": "S ",
}


@dataclass(frozen=True)
class GeocodeResult:
    lng: float
    lat: float
    matched_address: str
    geocoder: str


def normalize_address(raw: str) -> str:
    """Tidy a hand-typed address and append the city if it's missing."""
    addr = " ".join((raw or "").split()).strip(" ,")
    if not addr:
        return ""
    for pattern, repl in _ABBREVIATIONS.items():
        addr = re.sub(pattern, repl, addr, flags=re.IGNORECASE)
    # "Decatur and Bienville" -> "Decatur & Bienville" (Census wants the ampersand).
    addr = re.sub(r"\s+and\s+", " & ", addr, flags=re.IGNORECASE)
    if "new orleans" not in addr.lower():
        addr += CITY_SUFFIX
    return addr


def _get_json(url: str, params: dict, timeout: float = 20) -> dict | list:
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts
        return json.load(resp)


def _in_bbox(lng: float, lat: float) -> bool:
    west, south, east, north = settings.RETINA["BBOX"]
    return west <= lng <= east and south <= lat <= north


def geocode_census(address: str) -> GeocodeResult | None:
    data = _get_json(CENSUS_URL, {"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    m = matches[0]
    lng, lat = float(m["coordinates"]["x"]), float(m["coordinates"]["y"])
    if not _in_bbox(lng, lat):
        return None
    return GeocodeResult(lng, lat, m.get("matchedAddress", address), "census")


_last_nominatim = 0.0


def geocode_nominatim(address: str) -> GeocodeResult | None:
    global _last_nominatim
    wait = 1.1 - (time.monotonic() - _last_nominatim)
    if wait > 0:
        time.sleep(wait)
    west, south, east, north = settings.RETINA["BBOX"]
    _last_nominatim = time.monotonic()
    data = _get_json(
        NOMINATIM_URL,
        {
            "q": address,
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "us",
            "viewbox": f"{west},{north},{east},{south}",
            "bounded": 1,
        },
    )
    if not data:
        return None
    m = data[0]
    return GeocodeResult(float(m["lon"]), float(m["lat"]), m.get("display_name", address), "nominatim")


def geocode(address: str) -> GeocodeResult | None:
    """Census first, Nominatim if Census has nothing. Returns None if both miss."""
    addr = normalize_address(address)
    if not addr:
        return None
    for fn in (geocode_census, geocode_nominatim):
        try:
            result = fn(addr)
        except Exception as exc:  # network / parse errors: try the next geocoder
            log.warning("%s failed for %r: %s", fn.__name__, addr, exc)
            continue
        if result:
            return result
    return None
