import json

import pytest
import requests_mock

from sync_osm import handler as so

NODE = {
    "type": "node",
    "id": 987654321,
    "lat": 29.9647,
    "lon": -90.0545,
    "tags": {
        "man_made": "surveillance",
        "surveillance": "outdoor",
        "surveillance:type": "camera",
        "operator": "Project NOLA",
        "manufacturer": "Axis",
        "camera:direction": "135",
        "camera:mount": "pole",
        "addr:housenumber": "1200",
        "addr:street": "Magazine St",
    },
}


class TestTransform:
    @pytest.mark.parametrize(
        "tags,expected",
        [
            ({"surveillance:type": "ALPR"}, "alpr"),
            ({"surveillance:type": "alpr", "operator": "Project NOLA"}, "alpr"),
            ({"operator": "project nola"}, "project_nola"),
            ({"brand": "ProjectNOLA"}, "unknown"),
            ({"name": "Project NOLA cam 12"}, "project_nola"),
            ({"operator": "NOPD"}, "nopd"),
            ({"operator": "New Orleans Police Department"}, "nopd"),
            ({"surveillance": "traffic"}, "traffic"),
            ({}, "unknown"),
        ],
    )
    def test_camera_type(self, tags, expected):
        assert so.derive_camera_type(tags) == expected

    def test_direction(self):
        assert so.parse_direction({"camera:direction": "135"}) == 135
        assert so.parse_direction({"direction": "0"}) == 0
        assert so.parse_direction({"direction": "360"}) == 0
        assert so.parse_direction({"direction": "90-180"}) is None
        assert so.parse_direction({"direction": "400"}) is None
        assert so.parse_direction({"direction": "SE"}) == 135
        assert so.parse_direction({"direction": "forward"}) is None
        assert so.parse_direction({}) is None
        # camera:direction takes precedence over direction
        assert so.parse_direction({"camera:direction": "45", "direction": "90"}) == 45

    def test_mount(self):
        assert so.parse_mount({"camera:mount": "pole"}) == "pole"
        assert so.parse_mount({"camera:mount": "street_lamp"}) == "pole"
        assert so.parse_mount({"camera:mount": "wall"}) == "building"
        assert so.parse_mount({"camera:mount": "traffic_signals"}) == "traffic_signal"
        assert so.parse_mount({"camera:mount": "tree"}) == "other"
        assert so.parse_mount({}) == ""

    def test_facial_recognition(self):
        assert so.parse_facial_recognition({"surveillance:type": "FR"}) is True
        assert so.parse_facial_recognition({"surveillance:type": "facial"}) is True
        assert so.parse_facial_recognition({"surveillance:type": "camera"}) is False

    def test_street_address(self):
        assert so.parse_street_address(NODE["tags"]) == "1200 Magazine St"
        assert so.parse_street_address({"addr:street": "Canal St"}) == "Canal St"
        assert so.parse_street_address({}) == ""

    def test_build_row(self):
        row = so.build_camera_row(NODE)
        assert row["osm_id"] == 987654321 and row["source_ref"] == "987654321"
        assert row["source"] == "osm"
        assert row["camera_type"] == "project_nola"
        assert row["operator"] == "Project NOLA" and row["manufacturer"] == "Axis"
        assert row["direction"] == 135 and row["mount"] == "pole"
        assert row["facial_recognition"] is False
        assert row["street_address"] == "1200 Magazine St"
        assert json.loads(row["admin_notes"]) == NODE["tags"]
        assert "status" not in row

    def test_build_row_rejects_non_nodes(self):
        assert so.build_camera_row({"type": "way", "id": 1}) is None
        assert so.build_camera_row({"type": "node", "id": 1}) is None  # no lat/lon


class TestFetch:
    def test_sends_user_agent_and_query(self):
        with requests_mock.Mocker() as m:
            m.post(so.DEFAULT_OVERPASS_URL, json={"elements": [NODE]})
            elements = so.fetch_elements()
        assert elements == [NODE]
        req = m.request_history[0]
        assert req.headers["User-Agent"] == "retina-nola-camera-map/0.1 (+https://github.com/mwollenweber/retina)"
        body = req.text
        assert "around" in body and "16093" in body and "man_made" in body


class TestEndToEnd:
    def test_run_respects_ownership(self, fake_conn, monkeypatch):
        osm_owned = "22222222-2222-2222-2222-222222222222"
        admin_owned = "33333333-3333-3333-3333-333333333333"

        def find(sql, params):
            return {
                1: [{"id": osm_owned, "source": "osm", "status": "rejected"}],
                2: [{"id": admin_owned, "source": "field_report", "status": "vetted"}],
            }.get(params["osm_id"], [])

        fake_conn.on("select id, source, status from camera where osm_id", find)
        elements = [
            NODE,  # new -> created
            {**NODE, "id": 1},  # osm-owned, rejected -> updated, status untouched
            {**NODE, "id": 2},  # pushed from a field report -> skipped
            {"type": "way", "id": 5},  # ignored
        ]
        monkeypatch.setattr(so, "get_conn", lambda: fake_conn)
        with requests_mock.Mocker() as m:
            m.post(so.DEFAULT_OVERPASS_URL, json={"elements": elements})
            result = so.handler({}, None)

        assert result["seen"] == 4 and result["created"] == 1 and result["updated"] == 1 and result["skipped"] == 2

        inserts = fake_conn.statements("insert into camera")
        assert len(inserts) == 1
        assert inserts[0][1]["osm_id"] == 987654321 and inserts[0][1]["status"] == "vetted"

        updates = fake_conn.statements("update camera")
        assert len(updates) == 1
        sql, p = updates[0]
        assert p["id"] == osm_owned and p["osm_id"] == 1
        assert "status" not in p and "status =" not in sql
        assert all(params.get("id") != admin_owned for _, params in updates)

        assert fake_conn.sync_rows()[-1]["status"] == "succeeded"

    def test_overpass_406_marks_failed(self, fake_conn, monkeypatch):
        import requests

        monkeypatch.setattr(so, "get_conn", lambda: fake_conn)
        with requests_mock.Mocker() as m:
            m.post(so.DEFAULT_OVERPASS_URL, status_code=406)
            with pytest.raises(requests.HTTPError):
                so.handler({}, None)
        assert fake_conn.sync_rows()[-1]["status"] == "failed"
