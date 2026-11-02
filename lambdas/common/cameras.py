"""
Small SQL helpers for writing ``camera`` rows from the sync jobs.

A "row" here is a plain dict of column -> value, plus ``lon``/``lat`` floats
which are turned into the PostGIS geography point in SQL. No GDAL / shapely:
the point is built with ``ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography``.

Inserting fills every NOT NULL text column with '' so the row satisfies the
Django model's ``blank=True`` CharFields (which are NOT NULL in Postgres).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from common.schema import CAMERA_TABLE

# Text columns that are NOT NULL with default '' in the Django model.
TEXT_DEFAULT_EMPTY = (
    "street_address",
    "cross_street",
    "host_business",
    "operator",
    "manufacturer",
    "model_name",
    "mount",
    "source_ref",
    "reporter_contact",
    "reporter_notes",
    "admin_notes",
)

# Columns a sync job may write. ``lon``/``lat`` are handled separately.
WRITABLE_COLUMNS = {
    "street_address",
    "cross_street",
    "host_business",
    "camera_type",
    "operator",
    "manufacturer",
    "model_name",
    "facial_recognition",
    "direction",
    "mount",
    "source",
    "source_ref",
    "osm_id",
    "osm_pushed_at",
    "status",
    "admin_notes",
    "reported_at",
    "vetted_at",
}

POINT_SQL = "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography"


def find_by_source_ref(cur, source: str, source_ref: str) -> dict | None:
    cur.execute(
        f"SELECT id, source, status FROM {CAMERA_TABLE} WHERE source = %(source)s AND source_ref = %(ref)s",
        {"source": source, "ref": source_ref},
    )
    return cur.fetchone()


def find_by_osm_id(cur, osm_id: int) -> dict | None:
    cur.execute(
        f"SELECT id, source, status FROM {CAMERA_TABLE} WHERE osm_id = %(osm_id)s",
        {"osm_id": osm_id},
    )
    return cur.fetchone()


def _split(row: dict) -> tuple[dict, float, float]:
    cols = {k: v for k, v in row.items() if k in WRITABLE_COLUMNS}
    unknown = set(row) - WRITABLE_COLUMNS - {"lon", "lat"}
    if unknown:
        raise ValueError(f"unexpected camera columns: {sorted(unknown)}")
    return cols, float(row["lon"]), float(row["lat"])


def insert_camera(cur, row: dict, now: datetime) -> uuid.UUID:
    """INSERT a camera. Sets id, created_at/updated_at, and empty-string defaults."""
    cols, lon, lat = _split(row)
    for name in TEXT_DEFAULT_EMPTY:
        cols.setdefault(name, "")
    cols.setdefault("facial_recognition", False)
    cols.setdefault("camera_type", "unknown")
    cols.setdefault("status", "pending")
    cols.setdefault("reported_at", now)
    new_id = uuid.uuid4()
    params = {"id": new_id, "lon": lon, "lat": lat, "now": now, **cols}
    names = ["id", "location", "created_at", "updated_at", *cols]
    values = ["%(id)s", POINT_SQL, "%(now)s", "%(now)s", *[f"%({c})s" for c in cols]]
    cur.execute(
        f"INSERT INTO {CAMERA_TABLE} ({', '.join(names)}) VALUES ({', '.join(values)})",
        params,
    )
    return new_id


def update_camera(cur, camera_id, row: dict, now: datetime) -> None:
    """UPDATE the given columns (and location) on an existing camera; bumps updated_at."""
    cols, lon, lat = _split(row)
    sets = ["location = " + POINT_SQL, "updated_at = %(now)s"]
    sets += [f"{c} = %({c})s" for c in cols]
    params = {"id": camera_id, "lon": lon, "lat": lat, "now": now, **cols}
    cur.execute(f"UPDATE {CAMERA_TABLE} SET {', '.join(sets)} WHERE id = %(id)s", params)
