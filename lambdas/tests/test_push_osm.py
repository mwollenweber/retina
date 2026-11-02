import uuid

import pytest
import requests_mock

from push_osm import handler as po

CAM1 = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
CAM2 = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
CANDIDATES = [
    {"id": CAM1, "camera_type": "project_nola", "operator": "", "manufacturer": "Axis", "direction": 270, "mount": "pole", "lon": -90.07, "lat": 29.95},
    {"id": CAM2, "camera_type": "project_nola", "operator": "Some Bar <&>", "manufacturer": "", "direction": None, "mount": "other", "lon": -90.08, "lat": 29.96},
]


class TestTransform:
    def test_tags_default_operator(self):
        tags = po.build_node_tags(CANDIDATES[0])
        assert tags == {
            "man_made": "surveillance",
            "surveillance": "outdoor",
            "surveillance:type": "camera",
            "camera:type": "fixed",
            "source": "survey",
            "operator": "Project NOLA",
            "manufacturer": "Axis",
            "camera:direction": "270",
            "camera:mount": "pole",
        }

    def test_tags_camera_operator_and_optional_fields(self):
        tags = po.build_node_tags(CANDIDATES[1])
        assert tags["operator"] == "Some Bar <&>"
        assert "manufacturer" not in tags and "camera:direction" not in tags and "camera:mount" not in tags

    def test_xml_escaping(self):
        xml = po.node_xml(42, 29.96, -90.08, po.build_node_tags(CANDIDATES[1]))
        assert xml.startswith('<osm><node changeset="42" lat="29.9600000" lon="-90.0800000">')
        assert 'v="Some Bar &lt;&amp;&gt;"' in xml
        assert "<tag k=\"man_made\" v=\"surveillance\"/>" in xml

    def test_changeset_xml(self):
        xml = po.changeset_xml()
        assert 'k="created_by" v="retina"' in xml
        assert 'k="comment" v="Add surveillance cameras in New Orleans, surveyed in the field"' in xml

    def test_push_types_env(self):
        assert po.push_types_from_env({}) == ["project_nola"]
        assert po.push_types_from_env({"OSM_PUSH_TYPES": "project_nola, nopd"}) == ["project_nola", "nopd"]
        with pytest.raises(ValueError):
            po.push_types_from_env({"OSM_PUSH_TYPES": "bogus"})


@pytest.fixture
def db(fake_conn):
    fake_conn.on("select id, camera_type", CANDIDATES)
    return fake_conn


class TestEndToEnd:
    def test_dry_run_by_default(self, db, monkeypatch):
        monkeypatch.setattr(po, "get_conn", lambda: db)
        with requests_mock.Mocker() as m:  # any HTTP call would raise NoMockAddress
            result = po.handler({}, None)
        assert m.call_count == 0
        assert result["dry_run"] is True
        assert result["seen"] == 2 and result["created"] == 0
        assert [p["id"] for p in result["planned"]] == [str(CAM1), str(CAM2)]
        assert db.statements("update camera") == []
        sel = db.statements("select id, camera_type")[0][1]
        assert sel["types"] == ["project_nola"] and sel["limit"] == 25
        assert sel["sources"] == ["field_report", "admin"]
        assert db.sync_rows()[-1]["status"] == "succeeded"

    def test_enabled_without_token_fails(self, db, monkeypatch):
        monkeypatch.setenv("OSM_PUSH_ENABLED", "true")
        with pytest.raises(RuntimeError):
            po.run(db)
        assert db.sync_rows()[-1]["status"] == "failed"

    def test_push_creates_nodes_and_writes_back(self, db, monkeypatch):
        monkeypatch.setenv("OSM_PUSH_ENABLED", "true")
        monkeypatch.setenv("OSM_ACCESS_TOKEN", "secret-token")
        monkeypatch.setenv("OSM_API_URL", po.SANDBOX_API_URL)
        monkeypatch.setenv("OSM_PUSH_MAX", "10")
        monkeypatch.setattr(po, "get_conn", lambda: db)
        base = po.SANDBOX_API_URL

        with requests_mock.Mocker() as m:
            m.put(f"{base}/changeset/create", text="777")
            m.put(f"{base}/node/create", [{"text": "5001"}, {"text": "5002"}])
            m.put(f"{base}/changeset/777/close", text="")
            result = po.handler({}, None)

        assert result["dry_run"] is False
        assert result["changeset_id"] == 777
        assert result["pushed"] == [{"camera_id": str(CAM1), "osm_id": 5001}, {"camera_id": str(CAM2), "osm_id": 5002}]
        assert result["created"] == 2

        hist = m.request_history
        assert [r.path for r in hist] == ["/api/0.6/changeset/create", "/api/0.6/node/create", "/api/0.6/node/create", "/api/0.6/changeset/777/close"]
        assert all(r.headers["Authorization"] == "Bearer secret-token" for r in hist)
        assert 'changeset="777"' in hist[1].text and 'k="operator" v="Project NOLA"' in hist[1].text

        updates = db.statements("update camera")
        assert [(p["id"], p["osm_id"]) for _, p in updates] == [(CAM1, 5001), (CAM2, 5002)]
        assert "osm_pushed_at = %(now)s" in updates[0][0]
        assert db.sync_rows()[-1]["status"] == "succeeded"

    def test_mid_run_failure_closes_changeset_and_keeps_partial(self, db, monkeypatch):
        monkeypatch.setenv("OSM_PUSH_ENABLED", "true")
        monkeypatch.setenv("OSM_ACCESS_TOKEN", "t")
        monkeypatch.setenv("OSM_API_URL", po.SANDBOX_API_URL)
        base = po.SANDBOX_API_URL
        with requests_mock.Mocker() as m:
            m.put(f"{base}/changeset/create", text="778")
            m.put(f"{base}/node/create", [{"text": "6001"}, {"status_code": 409, "text": "conflict"}])
            close = m.put(f"{base}/changeset/778/close", text="")
            with pytest.raises(RuntimeError):
                po.run(db)
        assert close.called
        updates = db.statements("update camera")
        assert len(updates) == 1 and updates[0][1]["osm_id"] == 6001
        assert db.commits >= 2  # running row + per-node commit before the failure
        assert db.sync_rows()[-1]["status"] == "failed"
