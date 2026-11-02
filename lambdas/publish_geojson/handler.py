"""
publish_geojson: export every vetted camera (plus its approved photos) as a
GeoJSON FeatureCollection and upload it to S3 for the static frontend.

Privacy: ``reporter_contact``, ``reporter_notes``, ``admin_notes`` and
``submitted_by`` are never selected, so they cannot leak into the file.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from common.db import get_conn
from common.schema import (
    CAMERA_PHOTO_TABLE,
    CAMERA_PRIVATE_COLUMNS,
    CAMERA_TABLE,
    CameraStatus,
    PHOTO_PRIVATE_COLUMNS,
    PhotoStatus,
)
from common.sync_log import sync_run, utcnow

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SOURCE = "publish_geojson"
DEFAULT_DATA_KEY = "data/cameras.geojson"
CONTENT_TYPE = "application/geo+json"
CACHE_CONTROL = "public, max-age=300"

CAMERA_COLUMNS = (
    "id",
    "camera_type",
    "operator",
    "manufacturer",
    "model_name",
    "facial_recognition",
    "direction",
    "mount",
    "cross_street",
    "street_address",
    "host_business",
    "source",
    "osm_id",
    "vetted_at",
)
assert not set(CAMERA_COLUMNS) & set(CAMERA_PRIVATE_COLUMNS)

CAMERAS_SQL = f"""
SELECT {', '.join(CAMERA_COLUMNS)},
       ST_X(location::geometry) AS lon,
       ST_Y(location::geometry) AS lat
FROM {CAMERA_TABLE}
WHERE status = %(status)s
ORDER BY vetted_at NULLS LAST, id
"""

PHOTO_COLUMNS = ("camera_id", "image", "photo_type", "caption")
assert not set(PHOTO_COLUMNS) & set(PHOTO_PRIVATE_COLUMNS)

PHOTOS_SQL = f"""
SELECT {', '.join('p.' + c for c in PHOTO_COLUMNS)}
FROM {CAMERA_PHOTO_TABLE} p
JOIN {CAMERA_TABLE} c ON c.id = p.camera_id
WHERE p.status = %(photo_status)s AND c.status = %(camera_status)s
ORDER BY p.photo_type, p.submitted_at DESC
"""


# --------------------------------------------------------------------------
# Pure transformation
# --------------------------------------------------------------------------
def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def photo_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{str(key).lstrip('/')}"


def group_photos(photos: list[dict], base_url: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for p in photos:
        grouped.setdefault(str(p["camera_id"]), []).append(
            {
                "url": photo_url(base_url, p["image"]),
                "type": p.get("photo_type") or "",
                "caption": p.get("caption") or "",
            }
        )
    return grouped


def build_feature(camera: dict, photos: list[dict]) -> dict:
    cam_id = str(camera["id"])
    return {
        "type": "Feature",
        "id": cam_id,
        "geometry": {"type": "Point", "coordinates": [float(camera["lon"]), float(camera["lat"])]},
        "properties": {
            "id": cam_id,
            "camera_type": camera.get("camera_type") or "unknown",
            "operator": camera.get("operator") or "",
            "manufacturer": camera.get("manufacturer") or "",
            "model_name": camera.get("model_name") or "",
            "facial_recognition": bool(camera.get("facial_recognition")),
            "direction": camera.get("direction"),
            "mount": camera.get("mount") or "",
            "cross_street": camera.get("cross_street") or "",
            "street_address": camera.get("street_address") or "",
            "host_business": camera.get("host_business") or "",
            "source": camera.get("source") or "",
            "osm_id": camera.get("osm_id"),
            "vetted_at": _iso(camera.get("vetted_at")),
            "photos": photos,
        },
    }


def build_feature_collection(cameras: list[dict], photos: list[dict], base_url: str, generated_at: datetime | None = None) -> dict:
    generated_at = generated_at or utcnow()
    grouped = group_photos(photos, base_url)
    features = [build_feature(c, grouped.get(str(c["id"]), [])) for c in cameras]
    return {
        "type": "FeatureCollection",
        "generated_at": generated_at.isoformat(),
        "count": len(features),
        "features": features,
    }


def assert_no_private_fields(fc: dict) -> None:
    """Belt and braces: fail loudly if a private column ever ends up in the output."""
    forbidden = set(CAMERA_PRIVATE_COLUMNS) | set(PHOTO_PRIVATE_COLUMNS)
    for f in fc["features"]:
        leaked = forbidden & set(f["properties"])
        if leaked:
            raise RuntimeError(f"refusing to publish private fields: {sorted(leaked)}")


# --------------------------------------------------------------------------
# DB / IO
# --------------------------------------------------------------------------
def fetch_cameras(cur) -> list[dict]:
    cur.execute(CAMERAS_SQL, {"status": CameraStatus.VETTED})
    return list(cur.fetchall())


def fetch_photos(cur) -> list[dict]:
    cur.execute(PHOTOS_SQL, {"photo_status": PhotoStatus.APPROVED, "camera_status": CameraStatus.VETTED})
    return list(cur.fetchall())


def write_output(body: bytes, bucket: str | None, key: str, local_path: str | None, s3_client=None) -> str:
    if local_path:
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        with open(local_path, "wb") as fh:
            fh.write(body)
        return f"file://{os.path.abspath(local_path)}"
    if not bucket:
        raise RuntimeError("Set DATA_BUCKET (or LOCAL_OUTPUT_PATH for local dev)")
    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=CONTENT_TYPE,
        CacheControl=CACHE_CONTROL,
    )
    return f"s3://{bucket}/{key}"


def invalidate_cloudfront(distribution_id: str, paths: list[str], cf_client=None, reference: str | None = None) -> str:
    if cf_client is None:
        import boto3

        cf_client = boto3.client("cloudfront")
    reference = reference or f"retina-{utcnow().strftime('%Y%m%d%H%M%S%f')}"
    resp = cf_client.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={"Paths": {"Quantity": len(paths), "Items": paths}, "CallerReference": reference},
    )
    return resp.get("Invalidation", {}).get("Id", "")


def run(conn, s3_client=None, cf_client=None) -> dict:
    with sync_run(conn, SOURCE) as stats:
        base_url = os.environ.get("PHOTO_BASE_URL", "").rstrip("/")
        bucket = os.environ.get("DATA_BUCKET")
        key = os.environ.get("DATA_KEY", DEFAULT_DATA_KEY)
        local_path = os.environ.get("LOCAL_OUTPUT_PATH")
        distribution_id = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID")

        with conn.cursor() as cur:
            cameras = fetch_cameras(cur)
            photos = fetch_photos(cur)
        fc = build_feature_collection(cameras, photos, base_url)
        assert_no_private_fields(fc)
        stats.seen = len(cameras)
        stats.updated = len(cameras)  # "records written to the file"

        body = json.dumps(fc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        location = write_output(body, bucket, key, local_path, s3_client=s3_client)
        logger.info("published %d cameras / %d photos to %s (%d bytes)", len(cameras), len(photos), location, len(body))

        invalidation_id = None
        if distribution_id and not local_path:
            invalidation_id = invalidate_cloudfront(distribution_id, ["/" + key.lstrip("/")], cf_client=cf_client)
            logger.info("cloudfront invalidation %s created for %s", invalidation_id, distribution_id)

        return {
            **stats.as_dict(),
            "cameras": len(cameras),
            "photos": len(photos),
            "bytes": len(body),
            "location": location,
            "invalidation_id": invalidation_id,
        }


def handler(event, context):
    with get_conn() as conn:
        return run(conn)
