"""
sync_rtcc: pull the City of New Orleans "Public Safety Cameras" layer (the
Real-Time Crime Center camera map, published as the RTCC_Cameras ArcGIS
feature service behind nolagis.maps.arcgis.com) and upsert into ``camera``.

Upsert key: (source='city_rtcc', source_ref=OBJECTID). Official city data is
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

SOURCE = CameraSource.CITY_RTCC
DEFAULT_URL = (
    "https://services.arcgis.com/VhMjCzR3cIjEkh7L/arcgis/rest/services/RTCC_Cameras/FeatureServer/0/query"
)
PAGE_SIZE = 1000
OPERATOR = "City of New Orleans Real-Time Crime Center"
USER_AGENT = "retina-nola-camera-map/0.1 (+https://github.com/mwollenweber/retina)"


# --------------------------------------------------------------------------
# Pure transformation
# --------------------------------------------------------------------------
def _clean(value) -> str:
    return str(value or "").strip()


def parse_point(feature: dict) -> tuple[float, float] | None:
    """(lon, lat) from the GeoJSON geometry, falling back to the Lat/Long attributes."""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") if str(geom.get("type", "")).lower() == "point" else None
    if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
        props = feature.get("properties") or {}
        coords = (props.get("Long"), props.get("Lat"))
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90) or (lon == 0 and lat == 0):
        return None
    return lon, lat


def build_admin_notes(props: dict) -> str:
    head = "City public safety camera (nolagis RTCC_Cameras feature service)."
    details = []
    for key in ("Descr", "PD_Distric"):
        val = _clean(props.get(key))
        if val:
            details.append(f"{key}={val}")
    return f"{head} {'; '.join(details)}" if details else head


def build_camera_row(feature: dict) -> dict | None:
    """Map one GeoJSON feature to a camera row dict. None if no OBJECTID or point."""
    props = feature.get("properties") or {}
    oid = props.get("OBJECTID", feature.get("id"))
    point = parse_point(feature)
    if oid in (None, "") or point is None:
        return None
    lon, lat = point
    return {
        "lon": lon,
        "lat": lat,
        "source": SOURCE,
        "source_ref": str(oid),
        "camera_type": CameraType.NOPD,
        "operator": OPERATOR,
        "street_address": _clean(props.get("Unit"))[:255],
        "admin_notes": build_admin_notes(props),
    }


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def fetch_all(session: requests.Session | None = None, url: str | None = None):
    """Yield every feature from the ArcGIS layer, paging with resultOffset."""
    session = session or requests.Session()
    url = url or os.environ.get("RTCC_QUERY_URL", DEFAULT_URL)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "outSR": 4326,
            "f": "geojson",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }
        resp = session.get(url, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        page = resp.json()
        if "error" in page:
            raise RuntimeError(f"ArcGIS error: {str(page['error'])[:200]}")
        features = page.get("features")
        if not isinstance(features, list):
            raise RuntimeError(f"unexpected ArcGIS response: {str(page)[:200]}")
        yield from features
        exceeded = (page.get("properties") or {}).get("exceededTransferLimit", False)
        if not exceeded and len(features) < PAGE_SIZE:
            break
        if not features:
            break
        offset += len(features)


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
            for feature in fetch_all(session=session):
                stats.seen += 1
                row = build_camera_row(feature)
                if row is None:
                    skipped += 1
                    continue
                if upsert(cur, row, now) == "created":
                    stats.created += 1
                else:
                    stats.updated += 1
        logger.info("rtcc sync: %s (skipped without geom/id: %d)", stats.as_dict(), skipped)
        return {**stats.as_dict(), "skipped": skipped}


def handler(event, context):
    with get_conn() as conn:
        return run(conn)
