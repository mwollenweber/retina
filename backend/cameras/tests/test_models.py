import pytest
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError

from cameras.models import Camera, CameraStatus


@pytest.mark.django_db
def test_camera_requires_some_label():
    cam = Camera(location=Point(-90.07, 29.95, srid=4326))
    with pytest.raises(ValidationError):
        cam.full_clean()


@pytest.mark.django_db
def test_approve_and_reject_set_review_fields(pending_camera, admin_user):
    pending_camera.approve(admin_user)
    pending_camera.refresh_from_db()
    assert pending_camera.status == CameraStatus.VETTED
    assert pending_camera.vetted_by == admin_user
    assert pending_camera.vetted_at is not None

    pending_camera.reject(admin_user)
    pending_camera.refresh_from_db()
    assert pending_camera.status == CameraStatus.REJECTED


@pytest.mark.django_db
def test_lat_lng_properties(vetted_camera):
    assert round(vetted_camera.latitude, 4) == 29.9511
    assert round(vetted_camera.longitude, 4) == -90.0715
