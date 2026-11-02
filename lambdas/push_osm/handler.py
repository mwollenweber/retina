"""
push_osm: create OpenStreetMap nodes for field-surveyed cameras that are not
yet in OSM, and write the new node id back to ``camera.osm_id``.

NOTE ON OSM IMPORT GUIDELINES
-----------------------------
OSM's Import Guidelines (https://wiki.openstreetmap.org/wiki/Import/Guidelines)
and Automated Edits code of conduct expect community discussion (imports@ list,
local community) before bulk uploads, a dedicated import account, and clear
source documentation. This job therefore:

* is a DRY RUN by default (``OSM_PUSH_ENABLED=true`` to actually write),
* is capped at ``OSM_PUSH_MAX`` nodes per run (default 25),
* only pushes cameras a person vetted that came from field surveys or were
  entered by an admin (``source IN ('field_report','admin')``), and
* tags each node ``source=survey``.

Point it at the sandbox (``OSM_API_URL=https://master.apis.dev.openstreetmap.org/api/0.6``)
first. Keep it small and opt-in.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from xml.sax.saxutils import quoteattr

import requests

from common.db import get_conn
from common.schema import CAMERA_TABLE, CameraSource, CameraStatus, CameraType, Mount
from common.sync_log import sync_run, utcnow

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SOURCE = "push_osm"
DEFAULT_API_URL = "https://api.openstreetmap.org/api/0.6"
SANDBOX_API_URL = "https://master.apis.dev.openstreetmap.org/api/0.6"
DEFAULT_MAX = 25
DEFAULT_PUSH_TYPES = (CameraType.PROJECT_NOLA,)
PUSH_SOURCES = (CameraSource.FIELD_REPORT, CameraSource.ADMIN)
CHANGESET_COMMENT = "Add surveillance cameras in New Orleans, surveyed in the field"
CREATED_BY = "retina"
USER_AGENT = "retina-nola-camera-map/0.1 (+https://github.com/mwollenweber/retina)"

OPERATOR_BY_TYPE = {
    CameraType.PROJECT_NOLA: "Project NOLA",
}

# Our Mount choices -> OSM camera:mount values.
MOUNT_TO_OSM = {
    Mount.POLE: "pole",
    Mount.BUILDING: "wall",
    Mount.TRAFFIC_SIGNAL: "traffic_signals",
}

CANDIDATES_SQL = f"""
SELECT id, camera_type, operator, manufacturer, direction, mount,
       ST_X(location::geometry) AS lon,
       ST_Y(location::geometry) AS lat
FROM {CAMERA_TABLE}
WHERE status = %(status)s
  AND source = ANY(%(sources)s)
  AND osm_id IS NULL
  AND camera_type = ANY(%(types)s)
ORDER BY vetted_at NULLS LAST, id
LIMIT %(limit)s
"""

WRITEBACK_SQL = f"""
UPDATE {CAMERA_TABLE}
SET osm_id = %(osm_id)s, osm_pushed_at = %(now)s, updated_at = %(now)s
WHERE id = %(id)s
"""


# --------------------------------------------------------------------------
# Pure transformation
# --------------------------------------------------------------------------
def push_types_from_env(env: dict | None = None) -> list[str]:
    env = os.environ if env is None else env
    raw = env.get("OSM_PUSH_TYPES", "")
    types = [t.strip() for t in raw.split(",") if t.strip()]
    if not types:
        return list(DEFAULT_PUSH_TYPES)
    bad = [t for t in types if t not in CameraType.ALL]
    if bad:
        raise ValueError(f"OSM_PUSH_TYPES contains unknown camera types: {bad}")
    return types


def build_node_tags(camera: dict) -> dict[str, str]:
    tags = {
        "man_made": "surveillance",
        "surveillance": "outdoor",
        "surveillance:type": "camera",
        "camera:type": "fixed",
        "source": "survey",
    }
    operator = (camera.get("operator") or "").strip() or OPERATOR_BY_TYPE.get(camera.get("camera_type"), "")
    if operator:
        tags["operator"] = operator
    manufacturer = (camera.get("manufacturer") or "").strip()
    if manufacturer:
        tags["manufacturer"] = manufacturer
    direction = camera.get("direction")
    if direction is not None and 0 <= int(direction) <= 359:
        tags["camera:direction"] = str(int(direction))
    mount = MOUNT_TO_OSM.get(camera.get("mount") or "")
    if mount:
        tags["camera:mount"] = mount
    return tags


def _tags_xml(tags: dict[str, str]) -> str:
    return "".join(f"<tag k={quoteattr(k)} v={quoteattr(str(v))}/>" for k, v in tags.items())


def changeset_xml(comment: str = CHANGESET_COMMENT, created_by: str = CREATED_BY) -> str:
    tags = {"created_by": created_by, "comment": comment, "source": "survey"}
    return f'<osm><changeset>{_tags_xml(tags)}</changeset></osm>'


def node_xml(changeset_id: int, lat: float, lon: float, tags: dict[str, str]) -> str:
    return (
        f'<osm><node changeset="{int(changeset_id)}" lat="{float(lat):.7f}" lon="{float(lon):.7f}">'
        f"{_tags_xml(tags)}</node></osm>"
    )


def plan(cameras: list[dict]) -> list[dict]:
    """Dry-run view of what would be pushed."""
    return [
        {
            "id": str(c["id"]),
            "lat": float(c["lat"]),
            "lon": float(c["lon"]),
            "tags": build_node_tags(c),
        }
        for c in cameras
    ]


# --------------------------------------------------------------------------
# OSM API 0.6
# --------------------------------------------------------------------------
class OsmClient:
    def __init__(self, api_url: str, token: str, session: requests.Session | None = None):
        self.api_url = api_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "text/xml; charset=utf-8",
        }

    def _put(self, path: str, body: str = "") -> str:
        resp = self.session.put(f"{self.api_url}{path}", data=body.encode("utf-8"), headers=self.headers, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError(f"OSM API {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.text.strip()

    def open_changeset(self) -> int:
        return int(self._put("/changeset/create", changeset_xml()))

    def create_node(self, changeset_id: int, lat: float, lon: float, tags: dict[str, str]) -> int:
        return int(self._put("/node/create", node_xml(changeset_id, lat, lon, tags)))

    def close_changeset(self, changeset_id: int) -> None:
        self._put(f"/changeset/{changeset_id}/close")


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
def fetch_candidates(cur, types: list[str], limit: int) -> list[dict]:
    cur.execute(
        CANDIDATES_SQL,
        {"status": CameraStatus.VETTED, "sources": list(PUSH_SOURCES), "types": list(types), "limit": limit},
    )
    return list(cur.fetchall())


def write_back(cur, camera_id, osm_id: int, now: datetime) -> None:
    cur.execute(WRITEBACK_SQL, {"id": camera_id, "osm_id": osm_id, "now": now})


def run(conn, session: requests.Session | None = None) -> dict:
    with sync_run(conn, SOURCE) as stats:
        enabled = os.environ.get("OSM_PUSH_ENABLED", "").strip().lower() == "true"
        api_url = os.environ.get("OSM_API_URL", DEFAULT_API_URL)
        token = os.environ.get("OSM_ACCESS_TOKEN", "")
        limit = int(os.environ.get("OSM_PUSH_MAX", DEFAULT_MAX))
        types = push_types_from_env()

        with conn.cursor() as cur:
            cameras = fetch_candidates(cur, types, limit)
        stats.seen = len(cameras)
        planned = plan(cameras)

        if not enabled:
            for item in planned:
                logger.info("DRY RUN would create node at %.6f,%.6f tags=%s (camera %s)", item["lat"], item["lon"], item["tags"], item["id"])
            logger.info("DRY RUN: %d cameras would be pushed to %s. Set OSM_PUSH_ENABLED=true to push.", len(planned), api_url)
            return {**stats.as_dict(), "dry_run": True, "api_url": api_url, "planned": planned}

        if not cameras:
            return {**stats.as_dict(), "dry_run": False, "api_url": api_url, "changeset_id": None, "pushed": []}
        if not token:
            raise RuntimeError("OSM_PUSH_ENABLED=true but OSM_ACCESS_TOKEN is not set")

        client = OsmClient(api_url, token, session=session)
        now = utcnow()
        pushed = []
        changeset_id = client.open_changeset()
        logger.info("opened changeset %s on %s", changeset_id, api_url)
        try:
            with conn.cursor() as cur:
                for cam, item in zip(cameras, planned):
                    node_id = client.create_node(changeset_id, item["lat"], item["lon"], item["tags"])
                    write_back(cur, cam["id"], node_id, now)
                    # Commit per node: if a later node fails, every node that
                    # already exists in OSM is recorded locally (no orphans).
                    conn.commit()
                    stats.created += 1
                    pushed.append({"camera_id": item["id"], "osm_id": node_id})
                    logger.info("created node %s for camera %s", node_id, item["id"])
        finally:
            # Always close the changeset, even if a node failed midway.
            try:
                client.close_changeset(changeset_id)
            except Exception:  # pragma: no cover
                logger.exception("could not close changeset %s", changeset_id)
        return {**stats.as_dict(), "dry_run": False, "api_url": api_url, "changeset_id": changeset_id, "pushed": pushed}


def handler(event, context):
    with get_conn() as conn:
        return run(conn)
