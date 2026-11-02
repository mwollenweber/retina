import pytest
import requests
import requests_mock

from sync_rtcc import handler as sr


def feature(oid, lon=-90.0695, lat=29.9542, unit="116 Bourbon", descr=" ", district=" "):
    return {
        "type": "Feature",
        "id": oid,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "OBJECTID": oid, "Descr": descr, "Unit": unit, "Lat": lat, "Long": lon, "PD_Distric": district,
        },
    }


def page(features, exceeded=False):
    return {"type": "FeatureCollection", "features": features, "properties": {"exceededTransferLimit": exceeded}}


class TestTransform:
    def test_build_row(self):
        row = sr.build_camera_row(feature(7, descr="PTZ", district="8"))
        assert row["lon"] == -90.0695 and row["lat"] == 29.9542
        assert row["source"] == "city_rtcc" and row["source_ref"] == "7"
        assert row["camera_type"] == "nopd"
        assert row["operator"].startswith("City of New Orleans")
        assert row["street_address"] == "116 Bourbon"
        assert "Descr=PTZ" in row["admin_notes"] and "PD_Distric=8" in row["admin_notes"]
        assert "status" not in row

    def test_blank_attributes_are_dropped(self):
        row = sr.build_camera_row(feature(1))
        assert row["admin_notes"].endswith("feature service).")

    def test_falls_back_to_lat_long_attributes(self):
        f = feature(2)
        f["geometry"] = None
        assert sr.parse_point(f) == (-90.0695, 29.9542)

    def test_skips_without_id_or_point(self):
        f = feature(3)
        f["geometry"] = None
        f["properties"]["Lat"] = None
        assert sr.build_camera_row(f) is None
        f = feature(4, lon=0, lat=0)
        assert sr.build_camera_row(f) is None
        f = feature(5)
        del f["properties"]["OBJECTID"]
        del f["id"]
        assert sr.build_camera_row(f) is None


class TestFetch:
    def test_paginates_on_transfer_limit(self):
        page1 = page([feature(i) for i in range(sr.PAGE_SIZE)], exceeded=True)
        page2 = page([feature("last")])
        with requests_mock.Mocker() as m:
            m.get(sr.DEFAULT_URL, [{"json": page1}, {"json": page2}])
            rows = list(sr.fetch_all())
        assert len(rows) == sr.PAGE_SIZE + 1
        assert m.call_count == 2
        assert m.request_history[0].qs["resultoffset"] == ["0"]
        assert m.request_history[1].qs["resultoffset"] == [str(sr.PAGE_SIZE)]
        assert m.request_history[0].qs["f"] == ["geojson"]
        assert "retina" in m.request_history[0].headers["User-Agent"]

    def test_arcgis_error_body_raises(self):
        with requests_mock.Mocker() as m:
            m.get(sr.DEFAULT_URL, json={"error": {"code": 400, "message": "bad"}})
            with pytest.raises(RuntimeError, match="ArcGIS error"):
                list(sr.fetch_all())


class TestEndToEnd:
    def test_run_creates_and_updates(self, fake_conn, monkeypatch):
        existing_id = "11111111-1111-1111-1111-111111111111"

        def find(sql, params):
            if params["ref"] == "9":
                return [{"id": existing_id, "source": "city_rtcc", "status": "rejected"}]
            return []

        fake_conn.on("select id, source, status from camera where source = ", find)
        bad = feature(10)
        bad["geometry"] = None
        bad["properties"]["Lat"] = "x"
        monkeypatch.setattr(sr, "get_conn", lambda: fake_conn)
        with requests_mock.Mocker() as m:
            m.get(sr.DEFAULT_URL, json=page([feature(1), feature(9, unit="100 Blk of Royal"), bad]))
            result = sr.handler({}, None)

        assert result["seen"] == 3 and result["created"] == 1 and result["updated"] == 1 and result["skipped"] == 1
        inserts = fake_conn.statements("insert into camera")
        assert len(inserts) == 1
        p = inserts[0][1]
        assert p["source_ref"] == "1" and p["status"] == "vetted" and p["camera_type"] == "nopd"
        updates = fake_conn.statements("update camera")
        sql, p = updates[0]
        assert p["id"] == existing_id and p["street_address"] == "100 Blk of Royal"
        assert "status" not in p and "status =" not in sql
        finish = fake_conn.sync_rows()
        assert finish[-1]["status"] == "succeeded"

    def test_http_error_marks_run_failed(self, fake_conn, monkeypatch):
        monkeypatch.setattr(sr, "get_conn", lambda: fake_conn)
        with requests_mock.Mocker() as m:
            m.get(sr.DEFAULT_URL, status_code=503)
            with pytest.raises(requests.HTTPError):
                sr.handler({}, None)
        assert fake_conn.sync_rows()[-1]["status"] == "failed"
