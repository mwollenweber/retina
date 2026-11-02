import json
import uuid
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from publish_geojson import handler as pg

CAM_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
CAM_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
VETTED = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

CAMERAS = [
    {
        "id": CAM_A,
        "camera_type": "project_nola",
        "operator": "Project NOLA",
        "manufacturer": "",
        "facial_recognition": False,
        "direction": 90,
        "mount": "pole",
        "cross_street": "Canal St & Bourbon St",
        "street_address": "",
        "host_business": "Corner store",
        "source": "field_report",
        "osm_id": None,
        "vetted_at": VETTED,
        "lon": -90.0715,
        "lat": 29.9511,
    },
    {
        "id": CAM_B,
        "camera_type": "traffic",
        "operator": "City of New Orleans",
        "manufacturer": None,
        "facial_recognition": None,
        "direction": None,
        "mount": "",
        "cross_street": "",
        "street_address": "Claiborne Ave",
        "host_business": "",
        "source": "city_traffic",
        "osm_id": 123,
        "vetted_at": None,
        "lon": -90.08,
        "lat": 29.96,
    },
]
PHOTOS = [
    {"camera_id": CAM_A, "image": "photos/aaa/1.jpg", "photo_type": "close_up", "caption": "Pole cam"},
    {"camera_id": CAM_A, "image": "/photos/aaa/2.jpg", "photo_type": "surrounding", "caption": ""},
]


class TestTransform:
    def test_feature_collection_shape(self):
        fc = pg.build_feature_collection(CAMERAS, PHOTOS, "https://cdn.example.org/", generated_at=VETTED)
        assert fc["type"] == "FeatureCollection"
        assert fc["count"] == 2 and len(fc["features"]) == 2
        assert fc["generated_at"] == "2026-10-31T12:00:00+00:00"

        a = fc["features"][0]
        assert a["id"] == str(CAM_A)
        assert a["geometry"] == {"type": "Point", "coordinates": [-90.0715, 29.9511]}
        props = a["properties"]
        assert props["camera_type"] == "project_nola"
        assert props["direction"] == 90 and props["mount"] == "pole"
        assert props["vetted_at"] == "2026-10-31T12:00:00+00:00"
        assert props["photos"] == [
            {"url": "https://cdn.example.org/photos/aaa/1.jpg", "type": "close_up", "caption": "Pole cam"},
            {"url": "https://cdn.example.org/photos/aaa/2.jpg", "type": "surrounding", "caption": ""},
        ]

        b = fc["features"][1]["properties"]
        assert b["photos"] == []
        assert b["osm_id"] == 123 and b["vetted_at"] is None
        assert b["manufacturer"] == "" and b["facial_recognition"] is False

    def test_never_includes_private_fields(self):
        fc = pg.build_feature_collection(
            [{**CAMERAS[0], "reporter_contact": "x@y", "reporter_notes": "n", "admin_notes": "a"}],
            [{**PHOTOS[0], "submitted_by": "someone"}],
            "https://cdn",
        )
        props = fc["features"][0]["properties"]
        for key in ("reporter_contact", "reporter_notes", "admin_notes", "submitted_by"):
            assert key not in props
            assert key not in props["photos"][0]
        pg.assert_no_private_fields(fc)
        assert json.dumps(fc).find("someone") == -1

    def test_sql_does_not_select_private_columns(self):
        for col in ("reporter_contact", "reporter_notes", "admin_notes", "submitted_by"):
            assert col not in pg.CAMERAS_SQL
            assert col not in pg.PHOTOS_SQL

    def test_json_is_serialisable(self):
        fc = pg.build_feature_collection(CAMERAS, PHOTOS, "https://cdn")
        json.dumps(fc)


@pytest.fixture
def db(fake_conn):
    fake_conn.on("select id, camera_type", CAMERAS)
    fake_conn.on("from camera_photo p", PHOTOS)
    return fake_conn


class TestEndToEnd:
    @mock_aws
    def test_writes_to_s3_with_headers(self, db, monkeypatch):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="retina-data")
        monkeypatch.setenv("DATA_BUCKET", "retina-data")
        monkeypatch.setenv("PHOTO_BASE_URL", "https://photos.example.org")
        monkeypatch.setattr(pg, "get_conn", lambda: db)

        result = pg.handler({}, None)

        assert result["location"] == "s3://retina-data/data/cameras.geojson"
        assert result["cameras"] == 2 and result["photos"] == 2
        assert result["invalidation_id"] is None
        obj = s3.get_object(Bucket="retina-data", Key="data/cameras.geojson")
        assert obj["ContentType"] == "application/geo+json"
        assert obj["CacheControl"] == "public, max-age=300"
        fc = json.loads(obj["Body"].read())
        assert fc["count"] == 2
        assert fc["features"][0]["properties"]["photos"][0]["url"] == "https://photos.example.org/photos/aaa/1.jpg"

        finish = db.sync_rows()
        assert finish[-1]["status"] == "succeeded" and finish[-1]["seen"] == 2

    @mock_aws
    def test_cloudfront_invalidation(self, db, monkeypatch):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")
        monkeypatch.setenv("DATA_BUCKET", "retina-data")
        monkeypatch.setenv("DATA_KEY", "data/cameras.geojson")
        monkeypatch.setenv("CLOUDFRONT_DISTRIBUTION_ID", "E123")

        calls = []

        class CF:
            def create_invalidation(self, **kw):
                calls.append(kw)
                return {"Invalidation": {"Id": "INV1"}}

        result = pg.run(db, cf_client=CF())
        assert result["invalidation_id"] == "INV1"
        assert calls[0]["DistributionId"] == "E123"
        assert calls[0]["InvalidationBatch"]["Paths"]["Items"] == ["/data/cameras.geojson"]

    def test_local_output_path(self, db, monkeypatch, tmp_path):
        out = tmp_path / "out" / "cameras.geojson"
        monkeypatch.setenv("LOCAL_OUTPUT_PATH", str(out))
        monkeypatch.setenv("CLOUDFRONT_DISTRIBUTION_ID", "E123")  # ignored for local
        monkeypatch.setattr(pg, "get_conn", lambda: db)
        result = pg.handler({}, None)
        assert result["location"] == f"file://{out}"
        assert result["invalidation_id"] is None
        assert json.loads(out.read_text())["count"] == 2

    def test_no_bucket_fails_run(self, db):
        with pytest.raises(RuntimeError):
            pg.run(db)
        assert db.sync_rows()[-1]["status"] == "failed"
