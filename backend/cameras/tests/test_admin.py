import pytest

from cameras.models import CameraStatus, PhotoStatus


@pytest.mark.django_db
def test_admin_approve_action(client, admin_user, pending_camera):
    client.force_login(admin_user)
    r = client.post(
        "/admin/cameras/camera/",
        {"action": "approve", "_selected_action": [str(pending_camera.id)]},
        follow=True,
    )
    assert r.status_code == 200
    pending_camera.refresh_from_db()
    assert pending_camera.status == CameraStatus.VETTED
    assert pending_camera.vetted_by == admin_user


@pytest.mark.django_db
def test_admin_pages_render(client, admin_user, vetted_camera, approved_photo):
    client.force_login(admin_user)
    for url in [
        "/admin/cameras/camera/",
        f"/admin/cameras/camera/{vetted_camera.id}/change/",
        "/admin/cameras/cameraphoto/",
        "/admin/cameras/sourcesync/",
    ]:
        assert client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_admin_approve_photos_action(client, admin_user, vetted_camera, jpeg_bytes):
    from django.core.files.base import ContentFile

    from cameras.models import CameraPhoto

    p = CameraPhoto(camera=vetted_camera, status=PhotoStatus.PENDING)
    p.image.save("p.jpg", ContentFile(jpeg_bytes), save=True)
    client.force_login(admin_user)
    client.post(
        "/admin/cameras/camera/",
        {"action": "approve_photos", "_selected_action": [str(vetted_camera.id)]},
        follow=True,
    )
    p.refresh_from_db()
    assert p.status == PhotoStatus.APPROVED
