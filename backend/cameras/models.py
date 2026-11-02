"""
Data model for Retina: surveillance cameras in New Orleans.

Table names are pinned with ``db_table`` because the scheduled Lambda jobs in
``lambdas/`` write to these tables directly with psycopg. If you change a
column here, update ``lambdas/common/schema.py`` to match.
"""

import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone


class CameraType(models.TextChoices):
    PROJECT_NOLA = "project_nola", "Project NOLA"
    NOPD = "nopd", "NOPD / RTCC"
    TRAFFIC = "traffic", "City traffic"
    ALPR = "alpr", "License plate reader"
    PRIVATE = "private", "Private"
    UNKNOWN = "unknown", "Unknown"


class CameraStatus(models.TextChoices):
    PENDING = "pending", "Pending review"
    VETTED = "vetted", "Vetted"
    REJECTED = "rejected", "Rejected"


class CameraSource(models.TextChoices):
    FIELD_REPORT = "field_report", "Field report (public submission)"
    ADMIN = "admin", "Entered by admin"
    CITY_TRAFFIC = "city_traffic", "data.nola.gov traffic cameras"
    OSM = "osm", "OpenStreetMap"
    RECORDS_REQUEST = "records_request", "Public records request"


class Mount(models.TextChoices):
    POLE = "pole", "Pole"
    BUILDING = "building", "Building"
    TRAFFIC_SIGNAL = "traffic_signal", "Traffic signal"
    OTHER = "other", "Other"
    UNKNOWN = "", "Unknown"


class Camera(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Where
    location = models.PointField(srid=4326, geography=True, help_text="WGS84 point")
    street_address = models.CharField(max_length=255, blank=True)
    cross_street = models.CharField(
        max_length=255, blank=True, help_text="Nearest intersection, e.g. 'Canal St & Bourbon St'"
    )
    host_business = models.CharField(
        max_length=255, blank=True, help_text="Business or property hosting the camera"
    )

    # What
    camera_type = models.CharField(
        max_length=20, choices=CameraType.choices, default=CameraType.UNKNOWN, db_index=True
    )
    operator = models.CharField(max_length=255, blank=True)
    manufacturer = models.CharField(max_length=255, blank=True)
    model_name = models.CharField(max_length=255, blank=True)
    facial_recognition = models.BooleanField(default=False)
    direction = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Compass bearing the camera faces, 0-359"
    )
    mount = models.CharField(max_length=20, choices=Mount.choices, blank=True, default="")

    # Provenance
    source = models.CharField(
        max_length=20, choices=CameraSource.choices, default=CameraSource.FIELD_REPORT, db_index=True
    )
    source_ref = models.CharField(
        max_length=255, blank=True, help_text="External identifier, e.g. city camid"
    )
    osm_id = models.BigIntegerField(
        null=True, blank=True, unique=True, help_text="OpenStreetMap node id, if known"
    )
    osm_pushed_at = models.DateTimeField(null=True, blank=True)

    # Review
    status = models.CharField(
        max_length=20, choices=CameraStatus.choices, default=CameraStatus.PENDING, db_index=True
    )
    reporter_contact = models.CharField(
        max_length=255, blank=True, help_text="Optional email so we can follow up. Never published."
    )
    reporter_notes = models.TextField(blank=True)
    admin_notes = models.TextField(blank=True)
    reported_at = models.DateTimeField(default=timezone.now)
    vetted_at = models.DateTimeField(null=True, blank=True)
    vetted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vetted_cameras",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "camera"
        ordering = ["-reported_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_ref"],
                condition=~models.Q(source_ref=""),
                name="uniq_source_ref",
            ),
            models.CheckConstraint(
                condition=models.Q(direction__isnull=True) | models.Q(direction__lte=359),
                name="direction_range",
            ),
        ]

    def __str__(self):
        label = self.cross_street or self.street_address or self.host_business or str(self.id)[:8]
        return f"{self.get_camera_type_display()} @ {label}"

    def clean(self):
        if not any([self.cross_street, self.street_address, self.host_business]):
            raise ValidationError("Provide at least one of cross street, address, or host business.")

    @property
    def latitude(self):
        return self.location.y if self.location else None

    @property
    def longitude(self):
        return self.location.x if self.location else None

    def approve(self, user=None):
        self.status = CameraStatus.VETTED
        self.vetted_at = timezone.now()
        self.vetted_by = user
        self.save(update_fields=["status", "vetted_at", "vetted_by", "updated_at"])

    def reject(self, user=None):
        self.status = CameraStatus.REJECTED
        self.vetted_at = timezone.now()
        self.vetted_by = user
        self.save(update_fields=["status", "vetted_at", "vetted_by", "updated_at"])


def _photo_upload_to(instance, filename):
    ext = Path(filename).suffix.lower() or ".jpg"
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return f"photos/{instance.camera_id}/{stamp}-{uuid.uuid4().hex[:8]}{ext}"


class PhotoType(models.TextChoices):
    CLOSE_UP = "close_up", "Close up of the camera"
    SURROUNDING = "surrounding", "Surroundings / context"
    SIGNAGE = "signage", "Signage"


class PhotoStatus(models.TextChoices):
    PENDING = "pending", "Pending review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class CameraPhoto(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to=_photo_upload_to)
    photo_type = models.CharField(max_length=20, choices=PhotoType.choices, default=PhotoType.CLOSE_UP)
    status = models.CharField(
        max_length=20, choices=PhotoStatus.choices, default=PhotoStatus.PENDING, db_index=True
    )
    caption = models.CharField(max_length=255, blank=True)
    submitted_by = models.CharField(max_length=255, blank=True, help_text="Never published")
    submitted_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_photos",
    )

    class Meta:
        db_table = "camera_photo"
        ordering = ["photo_type", "-submitted_at"]

    def __str__(self):
        return f"{self.get_photo_type_display()} for {self.camera_id} ({self.status})"

    def approve(self, user=None):
        self.status = PhotoStatus.APPROVED
        self.reviewed_at = timezone.now()
        self.reviewed_by = user
        self.save(update_fields=["status", "reviewed_at", "reviewed_by"])

    def reject(self, user=None):
        self.status = PhotoStatus.REJECTED
        self.reviewed_at = timezone.now()
        self.reviewed_by = user
        self.save(update_fields=["status", "reviewed_at", "reviewed_by"])


class SourceSync(models.Model):
    """One row per scheduled ingestion run. Written by the Lambdas, read in admin."""

    class RunStatus(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.BigAutoField(primary_key=True)
    source = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    records_seen = models.IntegerField(default=0)
    records_created = models.IntegerField(default=0)
    records_updated = models.IntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        db_table = "source_sync"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.source} {self.started_at:%Y-%m-%d %H:%M} ({self.status})"
