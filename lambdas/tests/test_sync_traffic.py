import requests_mock

from sync_traffic import handler as st

GEOM = {"type": "Point", "coordinates": [-90.0715, 29.9511]}
RAW = {
    "the_geom": GEOM,
    "camid": "CAM-042",
    "camloc": "Canal St & Bourbon St",
    "dir": "SB",
    "active": "Yes",
    "function": "Traffic monitoring",
    "school": "No",
    "install": "2019-03-01T00:00:00.000",
    "operational": "Yes",
    "cat": "PTZ",
}


class TestTransform:
    def test_direction_mapping(self):
        assert st.parse_direction("SB") == 180
        assert st.parse_direction("NB") == 0
        assert st.parse_direction("EB") == 90
        assert st.parse_direction("WB") == 270
        assert st.parse_direction("ne") == 45
        assert st.parse_direction("NEB") == 45
        assert st.parse_direction("") is None
        assert st.parse_direction(None) is None
        assert st.parse_direction("ALL") is None

    def test_parse_point(self):
        assert st.parse_point(GEOM) == (-90.0715, 29.9511)
        assert st.parse_point(None) is None
        assert st.parse_point({"type": "Point", "coordinates": ["x", 1]}) is None
        assert st.parse_point({"type": "Point", "coordinates": [500, 1]}) is None
        assert st.parse_point({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}) is None

    def test_build_row(self):
        row = st.build_camera_row(RAW)
        assert row["lon"] == -90.0715 and row["lat"] == 29.9511
        assert row["source"] == "city_traffic" and row["source_ref"] == "CAM-042"
        assert row["camera_type"] == "traffic"
        assert row["operator"] == "City of New Orleans"
        assert row["street_address"] == "Canal St & Bourbon St"
        assert row["direction"] == 180
        assert row["admin_notes"].startswith("City traffic camera")
        assert "function=Traffic monitoring" in row["admin_notes"]
        assert "school=No" in row["admin_notes"]
        assert "INACTIVE" not in row["admin_notes"]
        assert "status" not in row  # status decided at insert time only

    def test_inactive_row_is_kept_and_noted(self):
        row = st.build_camera_row({**RAW, "active": "No"})
        assert row is not None
        assert "INACTIVE per city data" in row["admin_notes"]

    def test_skips_without_geom_or_camid(self):
        assert st.build_camera_row({**RAW, "the_geom": None}) is None
        assert st.build_camera_row({k: v for k, v in RAW.items() if k != "the_geom"}) is None
        assert st.build_camera_row({**RAW, "camid": ""}) is None


class TestFetch:
    def test_paginates_and_sends_token(self, monkeypatch):
        monkeypatch.setenv("SOCRATA_APP_TOKEN", "tok")
        page1 = [{"camid": str(i)} for i in range(st.PAGE_SIZE)]
        page2 = [{"camid": "last"}]
        with requests_mock.Mocker() as m:
            m.get(st.DEFAULT_URL, [{"json": page1}, {"json": page2}])
            rows = list(st.fetch_all())
        assert len(rows) == st.PAGE_SIZE + 1
        assert m.call_count == 2
        assert m.request_history[0].qs["$offset"] == ["0"]
        assert m.request_history[1].qs["$offset"] == [str(st.PAGE_SIZE)]
        assert m.request_history[0].headers["X-App-Token"] == "tok"
        assert "retina" in m.request_history[0].headers["User-Agent"]


class TestEndToEnd:
    def test_run_creates_and_updates(self, fake_conn, monkeypatch):
        existing_id = "11111111-1111-1111-1111-111111111111"

        def find(sql, params):
            if params["ref"] == "EXISTING":
                return [{"id": existing_id, "source": "city_traffic", "status": "rejected"}]
            return []

        fake_conn.on("select id, source, status from camera where source = ", find)
        data = [
            RAW,
            {**RAW, "camid": "EXISTING", "dir": "NB", "active": "No"},
            {**RAW, "camid": "NOGEOM", "the_geom": None},
        ]
        monkeypatch.setattr(st, "get_conn", lambda: fake_conn)
        with requests_mock.Mocker() as m:
            m.get(st.DEFAULT_URL, json=data)
            result = st.handler({}, None)

        assert result["seen"] == 3 and result["created"] == 1 and result["updated"] == 1 and result["skipped"] == 1

        inserts = fake_conn.statements("insert into camera")
        assert len(inserts) == 1
        p = inserts[0][1]
        assert p["source_ref"] == "CAM-042" and p["status"] == "vetted"
        assert p["vetted_at"] is not None and p["reported_at"] == p["vetted_at"]
        assert "ST_MakePoint" in inserts[0][0]

        updates = fake_conn.statements("update camera")
        assert len(updates) == 1
        sql, p = updates[0]
        assert p["id"] == existing_id and p["direction"] == 0
        assert "INACTIVE" in p["admin_notes"]
        assert "status" not in p and "status =" not in sql  # rejected stays rejected
        assert "updated_at = %(now)s" in sql

        finish = fake_conn.sync_rows()
        assert finish[-1]["status"] == "succeeded"
        assert (finish[-1]["seen"], finish[-1]["created"], finish[-1]["updated"]) == (3, 1, 1)

    def test_http_error_marks_run_failed(self, fake_conn, monkeypatch):
        import pytest
        import requests

        monkeypatch.setattr(st, "get_conn", lambda: fake_conn)
        with requests_mock.Mocker() as m:
            m.get(st.DEFAULT_URL, status_code=503)
            with pytest.raises(requests.HTTPError):
                st.handler({}, None)
        finish = fake_conn.sync_rows()
        assert finish[-1]["status"] == "failed"
        assert "503" in finish[-1]["error"]
        assert fake_conn.statements("insert into camera") == []
