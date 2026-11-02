import io

from PIL import Image

from cameras.images import process_upload, read_gps
from cameras.tests.conftest import make_jpeg


class FakeUpload(io.BytesIO):
    name = "IMG_0001.HEIC.jpg"


def test_gps_is_read_then_stripped(settings):
    raw = make_jpeg(with_gps=(29.9511, -90.0715))
    assert read_gps(Image.open(io.BytesIO(raw))) is not None

    out = process_upload(FakeUpload(raw), name_hint="IMG_0001.jpg")
    assert out.gps is not None
    lat, lon = out.gps
    assert abs(lat - 29.9511) < 1e-3 and abs(lon + 90.0715) < 1e-3

    reencoded = Image.open(out.file)
    assert reencoded.getexif().get_ifd(0x8825) == {}
    assert reencoded.getexif().get(0x0110) is None
    assert out.file.content_type == "image/jpeg"
    assert out.file.name.endswith(".jpg")


def test_downscale(settings):
    settings.RETINA["PHOTO_MAX_EDGE"] = 100
    raw = make_jpeg(size=(400, 200))
    out = process_upload(FakeUpload(raw))
    w, h = Image.open(out.file).size
    assert max(w, h) == 100


def test_rejects_non_image():
    try:
        process_upload(FakeUpload(b"not an image"))
    except ValueError:
        return
    raise AssertionError("expected ValueError")
