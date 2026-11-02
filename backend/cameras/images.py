"""
Photo handling. Every uploaded image is decoded and re-encoded with Pillow so
that EXIF (GPS, device serial, timestamps) never reaches storage. GPS is read
out first and returned so the caller can use it as a location hint.
"""

import io
from dataclasses import dataclass

from django.conf import settings
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError

GPS_IFD = 0x8825


@dataclass
class ProcessedImage:
    file: InMemoryUploadedFile
    gps: tuple[float, float] | None  # (lat, lon)


def _dms_to_deg(dms, ref):
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60 + s / 3600
    if ref in ("S", "W"):
        deg = -deg
    return deg


def read_gps(img: Image.Image) -> tuple[float, float] | None:
    try:
        exif = img.getexif()
        gps = exif.get_ifd(GPS_IFD)
    except Exception:
        return None
    if not gps:
        return None
    lat = _dms_to_deg(gps.get(2), gps.get(1))
    lon = _dms_to_deg(gps.get(4), gps.get(3))
    if lat is None or lon is None:
        return None
    return (lat, lon)


def process_upload(uploaded, *, name_hint="photo") -> ProcessedImage:
    """
    Decode, fix orientation, downscale, strip metadata, return a JPEG upload.
    Raises ValueError for non-images.
    """
    cfg = settings.RETINA
    try:
        img = Image.open(uploaded)
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("Unsupported or corrupt image") from e

    gps = read_gps(img)

    # Apply EXIF orientation before we throw EXIF away.
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    max_edge = cfg["PHOTO_MAX_EDGE"]
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)

    buf = io.BytesIO()
    # No `exif=` argument: metadata is dropped on save.
    img.save(buf, format="JPEG", quality=cfg["PHOTO_JPEG_QUALITY"], optimize=True)
    size = buf.tell()
    buf.seek(0)

    base = name_hint.rsplit(".", 1)[0] or "photo"
    out = InMemoryUploadedFile(buf, None, f"{base}.jpg", "image/jpeg", size, None)
    return ProcessedImage(file=out, gps=gps)
