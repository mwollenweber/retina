"""
Builds the public GeoJSON. Shared by the API view, the management command and
the admin "publish" action. The Lambda in lambdas/publish_geojson produces the
same shape without Django; keep the two in sync.
"""

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from .models import Camera, CameraPhoto, CameraStatus, PhotoStatus

PUBLIC_PROPS = (
    "camera_type",
    "operator",
    "manufacturer",
    "model_name",
    "facial_recognition",
    "direction",
    "mount",
    "cross_street",
    "street_address",
    "host_business",
    "source",
    "osm_id",
)


def vetted_queryset():
    return (
        Camera.objects.filter(status=CameraStatus.VETTED)
        .prefetch_related(
            Prefetch(
                "photos",
                queryset=CameraPhoto.objects.filter(status=PhotoStatus.APPROVED),
                to_attr="approved_photos",
            )
        )
        .order_by("-vetted_at")
    )


def photo_url(photo: CameraPhoto) -> str:
    base = settings.MEDIA_URL
    name = photo.image.name
    if base.startswith("http"):
        return f"{base.rstrip('/')}/{name}"
    try:
        return photo.image.url
    except Exception:
        return f"{base.rstrip('/')}/{name}"


def camera_to_feature(cam: Camera) -> dict:
    props = {k: getattr(cam, k) for k in PUBLIC_PROPS}
    props["vetted_at"] = cam.vetted_at.isoformat() if cam.vetted_at else None
    photos = getattr(cam, "approved_photos", None)
    if photos is None:
        photos = cam.photos.filter(status=PhotoStatus.APPROVED)
    props["photos"] = [{"url": photo_url(p), "type": p.photo_type, "caption": p.caption} for p in photos]
    return {
        "type": "Feature",
        "id": str(cam.id),
        "geometry": {"type": "Point", "coordinates": [cam.longitude, cam.latitude]},
        "properties": props,
    }


def build_feature_collection(queryset=None) -> dict:
    qs = vetted_queryset() if queryset is None else queryset
    features = [camera_to_feature(c) for c in qs]
    return {
        "type": "FeatureCollection",
        "generated_at": timezone.now().isoformat(),
        "count": len(features),
        "features": features,
    }
