"""
sync_osm: pull ``man_made=surveillance`` nodes inside Orleans Parish from
Overpass and upsert into ``camera`` keyed on ``osm_id``.

Rules:
* Only rows with source='osm' are updated. A camera that an admin entered or
  that came from a field report and was later pushed to OSM (so it has an
  osm_id) is left alone.
* ``status`` is never changed on update, so admin rejections stick.
* All OSM tags are kept as JSON in ``admin_notes`` for the reviewer.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import requests

from common.cameras import find_by_osm_id, insert_camera, update_camera
from common.db import get_conn
from common.schema import CameraSource, CameraStatus, CameraType, Mount
from common.sync_log import sync_run, utcnow

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SOURCE = CameraSource.OSM
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "retina-nola-camera-map/0.1 (+https://github.com/mwollenweber/retina)"

# Orleans Parish = OSM relation 1836428; Overpass area id = relation id + 3600000000.
ORLEANS_PARISH_AREA = 3601836428
OVERPASS_QUERY = f"""
[out:json][timeout:90];
area({ORLEANS_PARISH_AREA})->.a;
node["man_made"="surveillance"](area.a);
out body;
"""

COMPASS = {
    "N": 0, "NNE": 22, "NE": 45, "ENE": 67,
    "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
    "S": 180, "SSW": 202, "SW": 225, "WSW": 247,
    "W": 270, "WNW": 292, "NW": 315, "NNW": 337,
}

MOUNT_MAP = {
    "pole": Mount.POLE,
    "street_lamp": Mount.POLE,
    "lamp_post": Mount.POLE,
    "mast": Mount.POLE,
    "post": Mount.POLE,
    "wall": Mount.BUILDING,
    "building": Mount.BUILDING,
    "ceiling": Mount.BUILDING,
    "roof": Mount.BUILDING,
    "traffic_signals": Mount.TRAFFIC_SIGNAL,
    "traffic_signal": Mount.TRAFFIC_SIGNAL,
}


# --------------------------------------------------------------------------
# Pure transformation
# --------------------------------------------------------------------------
def _lower(tags: dict, key: str) -> str:
    return str(tags.get(key) or "").strip().lower()


def derive_camera_type(tags: dict) -> str:
    if _lower(tags, "surveillance:type") == "alpr":
        return CameraType.ALPR
    for key in ("operator", "brand", "name"):
        if "project nola" in _lower(tags, key):
            return CameraType.PROJECT_NOLA
    op = _lower(tags, "operator")
    if "nopd" in op or "new orleans police" in op:
        return CameraType.NOPD
    if _lower(tags, "surveillance") == "traffic":
        return CameraType.TRAFFIC
    return CameraType.UNKNOWN


def parse_direction(tags: dict) -> int | None:
    """Integer 0-359 from ``direction`` or ``camera:direction``; ranges/junk -> None."""
    for key in ("camera:direction", "direction"):
        raw = str(tags.get(key) or "").strip()
        if not raw:
            continue
        try:
            value = int(float(raw))
        except ValueError:
            cardinal = COMPASS.get(raw.upper())
            if cardinal is not None:
                return cardinal
            continue  # e.g. "90-180", "all"
        if value == 360:
            value = 0
        if 0 <= value <= 359:
            return value
    return None


def parse_mount(tags: dict) -> str:
    raw = _lower(tags, "camera:mount")
    if not raw:
        return Mount.UNKNOWN
    return MOUNT_MAP.get(raw, Mount.OTHER)


def parse_facial_recognition(tags: dict) -> bool:
    return _lower(tags, "surveillance:type") in ("fr", "face", "facial")


def parse_street_address(tags: dict) -> str:
    number = str(tags.get("addr:housenumber") or "").strip()
    street = str(tags.get("addr:street") or "").strip()
    return " ".join(p for p in (number, street) if p)[:255]


def build_camera_row(element: dict) -> dict | None:
    """Map one Overpass node to a camera row dict. Returns None if not a usable node."""
    if element.get("type") != "node":
        return None
    try:
        osm_id = int(element["id"])
        lon = float(element["lon"])
        lat = float(element["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    tags = element.get("tags") or {}
    return {
        "lon": lon,
        "lat": lat,
        "source": SOURCE,
        "source_ref": str(osm_id),
        "osm_id": osm_id,
        "camera_type": derive_camera_type(tags),
        "manufacturer": str(tags.get("manufacturer") or "").strip()[:255],
        "operator": str(tags.get("operator") or "").strip()[:255],
        "direction": parse_direction(tags),
        "mount": parse_mount(tags),
        "facial_recognition": parse_facial_recognition(tags),
        "street_address": parse_street_address(tags),
        "admin_notes": json.dumps(tags, sort_keys=True, ensure_ascii=False),
    }


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def fetch_elements(session: requests.Session | None = None, url: str | None = None) -> list[dict]:
    session = session or requests.Session()
    url = url or os.environ.get("OVERPASS_URL", DEFAULT_OVERPASS_URL)
    resp = session.post(
        url,
        data={"data": OVERPASS_QUERY},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    if "remark" in body:
        logger.warning("overpass remark: %s", body["remark"])
    return body.get("elements", [])


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
def upsert(cur, row: dict, now: datetime) -> str:
    """Returns 'created', 'updated', or 'skipped' (row exists but is not OSM-owned)."""
    existing = find_by_osm_id(cur, row["osm_id"])
    if existing is None:
        insert_camera(
            cur,
            {**row, "status": CameraStatus.VETTED, "reported_at": now, "vetted_at": now},
            now,
        )
        return "created"
    if existing.get("source") != SOURCE:
        return "skipped"
    update_camera(cur, existing["id"], row, now)  # row has no 'status' key
    return "updated"


def run(conn, session: requests.Session | None = None) -> dict:
    with sync_run(conn, SOURCE) as stats:
        now = utcnow()
        skipped = 0
        elements = fetch_elements(session=session)
        with conn.cursor() as cur:
            for element in elements:
                stats.seen += 1
                row = build_camera_row(element)
                if row is None:
                    skipped += 1
                    continue
                result = upsert(cur, row, now)
                if result == "created":
                    stats.created += 1
                elif result == "updated":
                    stats.updated += 1
                else:
                    skipped += 1
        logger.info("osm sync: %s (skipped: %d)", stats.as_dict(), skipped)
        return {**stats.as_dict(), "skipped": skipped}


def handler(event, context):
    with get_conn() as conn:
        return run(conn)
