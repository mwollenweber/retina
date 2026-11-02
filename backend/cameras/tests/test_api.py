import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from cameras.models import Camera, CameraStatus, PhotoStatus
from cameras.tests.conftest import make_jpeg

CAMERAS = "/api/cameras/"
REPORTS = "/api/reports/"


@pytest.mark.django_db
def test_collection_only_vetted_and_only_public_fields(client, vetted_camera, pending_camera, approved_photo):
    r = client.get(CAMERAS)
    assert r.status_code == 200
    assert r["Content-Type"].startswith("application/geo+json")
    body = r.json()
    assert body["type"] == "FeatureCollection"
    assert body["count"] == 1
    f = body["features"][0]
    assert f["id"] == str(vetted_camera.id)
    assert f["geometry"]["coordinates"][0] == pytest.approx(-90.0715)
    props = f["properties"]
    assert props["camera_type"] == "project_nola"
    assert props["facial_recognition"] is True
    assert len(props["photos"]) == 1 and props["photos"][0]["type"] == "close_up"
    for secret in ("reporter_contact", "reporter_notes", "admin_notes", "submitted_by"):
        assert secret not in props
    raw = r.content.decode()
    assert "secret@example.com" not in raw and "internal" not in raw


@pytest.mark.django_db
def test_collection_filters(client, vetted_camera, approved_photo):
    assert client.get(CAMERAS + "?camera_type=nopd").json()["count"] == 0
    assert client.get(CAMERAS + "?camera_type=project_nola,alpr").json()["count"] == 1
    assert client.get(CAMERAS + "?facial_recognition=false").json()["count"] == 0
    assert client.get(CAMERAS + "?has_photos=true").json()["count"] == 1
    assert client.get(CAMERAS + "?bbox=-90.1,29.9,-90.0,30.0").json()["count"] == 1
    assert client.get(CAMERAS + "?bbox=-91,28,-90.9,28.1").json()["count"] == 0
    assert client.get(CAMERAS + "?bbox=junk").status_code == 400


@pytest.mark.django_db
def test_detail_404_for_pending(client, vetted_camera, pending_camera):
    assert client.get(f"{CAMERAS}{vetted_camera.id}/").status_code == 200
    assert client.get(f"{CAMERAS}{pending_camera.id}/").status_code == 404


@pytest.mark.django_db
def test_report_creates_pending_camera_with_stripped_photos(client):
    raw = make_jpeg(with_gps=(29.9511, -90.0715))
    r = client.post(
        REPORTS,
        {
            "lat": "29.9511",
            "lng": "-90.0715",
            "camera_type": "project_nola",
            "host_business": "Corner Store",
            "facial_recognition": "true",
            "reporter_contact": "me@example.com",
            "photos": [
                SimpleUploadedFile("a.jpg", raw, "image/jpeg"),
                SimpleUploadedFile("b.jpg", raw, "image/jpeg"),
            ],
            "photo_types": ["close_up", "surrounding"],
        },
    )
    assert r.status_code == 201, r.content
    cam = Camera.objects.get(id=r.json()["id"])
    assert cam.status == CameraStatus.PENDING
    assert cam.source == "field_report"
    assert cam.photos.count() == 2
    assert {p.photo_type for p in cam.photos.all()} == {"close_up", "surrounding"}
    assert all(p.status == PhotoStatus.PENDING for p in cam.photos.all())
    assert all(p.submitted_by == "me@example.com" for p in cam.photos.all())
    # Nothing public until vetted
    assert client.get(CAMERAS).json()["count"] == 0

    from PIL import Image

    stored = cam.photos.first().image
    stored.open("rb")
    assert Image.open(io.BytesIO(stored.read())).getexif().get_ifd(0x8825) == {}


@pytest.mark.django_db
def test_report_location_from_photo_gps(client):
    raw = make_jpeg(with_gps=(29.96, -90.06))
    r = client.post(
        REPORTS,
        {"cross_street": "Esplanade & Decatur", "photos": [SimpleUploadedFile("a.jpg", raw, "image/jpeg")]},
    )
    assert r.status_code == 201, r.content
    cam = Camera.objects.get(id=r.json()["id"])
    assert abs(cam.latitude - 29.96) < 1e-3
    assert "photo GPS" in cam.reporter_notes


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload,needle",
    [
        ({"cross_street": "x"}, "location"),
        ({"lat": 40.7, "lng": -74.0, "cross_street": "x"}, "outside"),
        ({"lat": 29.95, "lng": -90.07}, "at least one"),
        ({"lat": 29.95, "lng": -90.07, "cross_street": "x", "website": "http://spam"}, "Rejected"),
        ({"lat": 29.95, "lng": -90.07, "cross_street": "x", "direction": 400}, "direction"),
    ],
)
def test_report_validation(client, payload, needle):
    r = client.post(REPORTS, payload)
    assert r.status_code == 400
    assert needle.lower() in r.content.decode().lower()


@pytest.mark.django_db
def test_report_rejects_non_image(client):
    r = client.post(
        REPORTS,
        {
            "lat": 29.95,
            "lng": -90.07,
            "cross_street": "x",
            "photos": [SimpleUploadedFile("a.jpg", b"nope", "image/jpeg")],
        },
    )
    assert r.status_code == 400


@pytest.mark.django_db
def test_healthz(client):
    r = client.get("/healthz/")
    assert r.status_code == 200 and r.json()["db"] == "ok"
