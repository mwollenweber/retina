import io

import pytest
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.utils import timezone
from PIL import Image

from cameras.models import Camera, CameraPhoto, CameraSource, CameraStatus, CameraType, PhotoStatus


def make_jpeg(size=(64, 48), with_gps=None, color=(200, 30, 30)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    kwargs = {}
    if with_gps:
        lat, lon = with_gps
        exif = Image.Exif()
        gps = {
            1: "N" if lat >= 0 else "S",
            2: _to_dms(abs(lat)),
            3: "E" if lon >= 0 else "W",
            4: _to_dms(abs(lon)),
        }
        exif[0x8825] = gps
        exif[0x0110] = "FakePhone 9"  # Model tag, should be stripped
        kwargs["exif"] = exif.tobytes()
    img.save(buf, format="JPEG", **kwargs)
    return buf.getvalue()


def _to_dms(deg):
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60, 4)
    return (d, m, s)


@pytest.fixture
def jpeg_bytes():
    return make_jpeg()


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_superuser("admin", "a@example.com", "pw")


@pytest.fixture
def vetted_camera(db):
    return Camera.objects.create(
        location=Point(-90.0715, 29.9511, srid=4326),
        camera_type=CameraType.PROJECT_NOLA,
        cross_street="Canal St & Bourbon St",
        host_business="Test Shop",
        facial_recognition=True,
        status=CameraStatus.VETTED,
        vetted_at=timezone.now(),
        source=CameraSource.ADMIN,
        reporter_contact="secret@example.com",
        reporter_notes="private",
        admin_notes="internal",
    )


@pytest.fixture
def pending_camera(db):
    return Camera.objects.create(
        location=Point(-90.08, 29.95, srid=4326),
        camera_type=CameraType.NOPD,
        cross_street="Poydras & Loyola",
        status=CameraStatus.PENDING,
    )


@pytest.fixture
def approved_photo(vetted_camera, jpeg_bytes):
    from django.core.files.base import ContentFile

    p = CameraPhoto(camera=vetted_camera, photo_type="close_up", status=PhotoStatus.APPROVED)
    p.image.save("x.jpg", ContentFile(jpeg_bytes), save=True)
    return p
