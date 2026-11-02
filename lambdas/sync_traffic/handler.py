"""
sync_traffic: pull the City of New Orleans traffic camera dataset from
data.nola.gov (Socrata dataset te2d-4txp) and upsert into ``camera``.

Upsert key: (source='city_traffic', source_ref=camid). Official city data is
stored as status='vetted' on insert. On update we refresh location and
descriptive columns but never touch ``status`` (an admin rejection sticks).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

from common.cameras import find_by_source_ref, insert_camera, update_camera
from common.db import get_conn
from common.schema import CameraSource, CameraStatus, CameraType
from common.sync_log import sync_run, utcnow

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SOURCE = CameraSource.CITY_TRAFFIC
DEFAULT_URL = "https://data.nola.gov/resource/te2d-4txp.json"
PAGE_SIZE = 1000
OPERATOR = "City of New Orleans"
USER_AGENT = "retina-nola-camera-map/0.1 (+https://github.com/mwollenweber/retina)"

COMPASS = {
    "N": 0,
    "NE": 45,
    "E": 90,
    "SE": 135,
    "S": 180,
    "SW": 225,
    "W": 270,
    "NW": 315,
}


# --------------------------------------------------------------------------
# Pure transformation
# --------------------------------------------------------------------------
def parse_direction(value) -> int | None:
    """'SB' -> 180, 'NB' -> 0, 'EB' -> 90, 'WB' -> 270, 'NE' -> 45. Unknown -> None."""
    if not value:
        return None
    key = str(value).strip().upper()
    if len(key) >= 2 and key.endswith("B") and key[:-1] in COMPASS:
        key = key[:-1]
    return COMPASS.get(key)


def parse_point(the_geom) -> tuple[float, float] | None:
    """Return (lon, lat) from a Socrata ``the_geom`` dict, or None."""
    if not isinstance(the_geom, dict):
        return None
    if str(the_geom.get("type", "")).lower() != "point":
        return None
    coords = the_geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def build_admin_notes(raw: dict) -> str:
    """One short line: 'City traffic camera (...). function=...; school=...; active=...'."""
    head = "City traffic camera (data.nola.gov te2d-4txp)."
    details = []
    for key in ("function", "cat", "school", "install", "operational", "active"):
        val = raw.get(key)
        if val not in (None, ""):
            details.append(f"{key}={str(val).strip()}")
    if str(raw.get("active", "")).strip().lower() == "no":
        details.append("INACTIVE per city data")
    return f"{head} {'; '.join(details)}" if details else head


def build_camera_row(raw: dict) -> dict | None:
    """
    Map one Socrata record to a camera row dict (columns + lon/lat).
    Returns None when the record has no usable geometry or camid.
    """
    point = parse_point(raw.get("the_geom"))
    camid = str(raw.get("camid") or "").strip()
    if point is None or not camid:
        return None
    lon, lat = point
    return {
        "lon": lon,
        "lat": lat,
        "source": SOURCE,
        "source_ref": camid,
        "camera_type": CameraType.TRAFFIC,
        "operator": OPERATOR,
        "street_address": str(raw.get("camloc") or "").strip()[:255],
        "direction": parse_direction(raw.get("dir")),
        "admin_notes": build_admin_notes(raw),
    }


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def fetch_all(session: requests.Session | None = None, url: str | None = None, app_token: str | None = None):
    """Yield every Socrata record, paginating with $limit/$offset."""
    session = session or requests.Session()
    url = url or os.environ.get("SOCRATA_URL", DEFAULT_URL)
    app_token = app_token if app_token is not None else os.environ.get("SOCRATA_APP_TOKEN", "")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if app_token:
        headers["X-App-Token"] = app_token
    offset = 0
    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        resp = session.get(url, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list):
            raise RuntimeError(f"unexpected Socrata response: {str(page)[:200]}")
        yield from page
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
def upsert(cur, row: dict, now: datetime) -> str:
    existing = find_by_source_ref(cur, row["source"], row["source_ref"])
    if existing is None:
        insert_camera(
            cur,
            {**row, "status": CameraStatus.VETTED, "reported_at": now, "vetted_at": now},
            now,
        )
        return "created"
    # Never change status/vetted_at on update so admin decisions stick.
    update_camera(cur, existing["id"], row, now)
    return "updated"


def run(conn, session: requests.Session | None = None) -> dict:
    with sync_run(conn, SOURCE) as stats:
        now = utcnow()
        skipped = 0
        with conn.cursor() as cur:
            for raw in fetch_all(session=session):
                stats.seen += 1
                row = build_camera_row(raw)
                if row is None:
                    skipped += 1
                    continue
                result = upsert(cur, row, now)
                if result == "created":
                    stats.created += 1
                else:
                    stats.updated += 1
        logger.info("traffic sync: %s (skipped without geom/camid: %d)", stats.as_dict(), skipped)
        return {**stats.as_dict(), "skipped": skipped}


def handler(event, context):
    with get_conn() as conn:
        return run(conn)
