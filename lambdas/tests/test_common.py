import json

import pytest

from common import db
from common.schema import CAMERA_TABLE, CameraSource, CameraStatus, CameraType, Mount, RunStatus
from common.sync_log import sync_run
from common.cameras import insert_camera, update_camera
from common.sync_log import utcnow


class TestSchema:
    def test_choices_mirror_models(self):
        assert CAMERA_TABLE == "camera"
        assert set(CameraType.ALL) == {"project_nola", "nopd", "traffic", "alpr", "private", "unknown"}
        assert set(CameraStatus.ALL) == {"pending", "vetted", "rejected"}
        assert set(CameraSource.ALL) == {"field_report", "admin", "city_traffic", "osm", "records_request"}
        assert set(Mount.ALL) == {"pole", "building", "traffic_signal", "other", ""}
        assert set(RunStatus.ALL) == {"running", "succeeded", "failed"}


class TestDb:
    def test_database_url_wins(self):
        assert db.resolve_dsn({"DATABASE_URL": "postgres://u:p@h/db", "DB_SECRET_ARN": "x"}) == "postgres://u:p@h/db"

    def test_missing_config(self):
        with pytest.raises(RuntimeError):
            db.resolve_dsn({})

    def test_secret_is_cached(self):
        db.clear_secret_cache()
        calls = []

        class Client:
            def get_secret_value(self, SecretId):
                calls.append(SecretId)
                return {"SecretString": json.dumps({"username": "ret ina", "password": "p@ss/w", "host": "db.example", "port": 5432, "dbname": "retina"})}

        env = {"DB_SECRET_ARN": "arn:secret"}
        dsn1 = db.resolve_dsn(env, secret_client=Client())
        dsn2 = db.resolve_dsn(env, secret_client=Client())
        assert dsn1 == dsn2 == "postgresql://ret%20ina:p%40ss%2Fw@db.example:5432/retina?sslmode=require"
        assert calls == ["arn:secret"]
        db.clear_secret_cache()


class TestSyncRun:
    def test_success_path(self, fake_conn):
        with sync_run(fake_conn, "city_traffic") as stats:
            stats.seen = 3
            stats.created = 1
            stats.updated = 2
        assert stats.id == 101
        inserts = fake_conn.statements("insert into source_sync")
        assert len(inserts) == 1 and inserts[0][1]["status"] == "running"
        finish = fake_conn.sync_rows()
        assert len(finish) == 1
        assert finish[0]["status"] == "succeeded"
        assert (finish[0]["seen"], finish[0]["created"], finish[0]["updated"]) == (3, 1, 2)
        assert finish[0]["error"] == ""
        assert finish[0]["id"] == 101
        assert fake_conn.commits == 2 and fake_conn.rollbacks == 0

    def test_failure_path_rolls_back_and_records_error(self, fake_conn):
        with pytest.raises(ValueError):
            with sync_run(fake_conn, "osm") as stats:
                stats.seen = 5
                raise ValueError("boom")
        finish = fake_conn.sync_rows()
        assert finish[0]["status"] == "failed"
        assert "ValueError: boom" in finish[0]["error"]
        assert finish[0]["seen"] == 5
        assert fake_conn.rollbacks == 1
        assert fake_conn.commits == 2  # running row + failed row

    def test_source_too_long(self, fake_conn):
        with pytest.raises(ValueError):
            with sync_run(fake_conn, "x" * 41):
                pass


class TestCameraWrites:
    def test_insert_builds_point_and_defaults(self, fake_conn):
        now = utcnow()
        cur = fake_conn.cursor()
        new_id = insert_camera(cur, {"lon": -90.07, "lat": 29.95, "source": "osm", "source_ref": "1", "osm_id": 1}, now)
        sql, params = fake_conn.executed[-1]
        assert "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography" in sql
        assert params["id"] == new_id
        assert params["lon"] == -90.07 and params["lat"] == 29.95
        assert params["admin_notes"] == "" and params["mount"] == ""
        assert params["status"] == "pending" and params["camera_type"] == "unknown"
        assert params["reported_at"] == now

    def test_update_rejects_unknown_columns(self, fake_conn):
        cur = fake_conn.cursor()
        with pytest.raises(ValueError):
            update_camera(cur, "abc", {"lon": 0, "lat": 0, "reporter_contact": "x"}, utcnow())

    def test_update_sets_updated_at(self, fake_conn):
        now = utcnow()
        cur = fake_conn.cursor()
        update_camera(cur, "abc", {"lon": 1.0, "lat": 2.0, "operator": "X"}, now)
        sql, params = fake_conn.executed[-1]
        assert sql.startswith("UPDATE camera SET location = ST_SetSRID")
        assert "updated_at = %(now)s" in sql and "operator = %(operator)s" in sql
        assert "status" not in sql
        assert params["id"] == "abc"
