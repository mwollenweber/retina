from django.conf import settings
from django.contrib.gis.geos import Point
from rest_framework import serializers

from .images import process_upload
from .models import Camera, CameraPhoto, CameraSource, CameraStatus, CameraType, Mount, PhotoType


class ReportSerializer(serializers.Serializer):
    """Public camera report. Everything is optional except a location (or a photo with GPS)."""

    lat = serializers.FloatField(required=False, min_value=-90, max_value=90)
    lng = serializers.FloatField(required=False, min_value=-180, max_value=180)
    camera_type = serializers.ChoiceField(choices=CameraType.choices, default=CameraType.PROJECT_NOLA)
    host_business = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    cross_street = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    street_address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    facial_recognition = serializers.BooleanField(required=False, default=False)
    direction = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=359)
    mount = serializers.ChoiceField(choices=Mount.choices, required=False, allow_blank=True, default="")
    reporter_contact = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    reporter_notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=4000)
    photos = serializers.ListField(
        child=serializers.ImageField(), required=False, allow_empty=True, max_length=8
    )
    photo_types = serializers.ListField(
        child=serializers.ChoiceField(choices=PhotoType.choices), required=False, allow_empty=True
    )
    # Honeypot: real users never fill this.
    website = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, data):
        if data.get("website"):
            raise serializers.ValidationError("Rejected.")
        cfg = settings.RETINA
        if len(data.get("photos", [])) > cfg["MAX_PHOTOS_PER_REPORT"]:
            raise serializers.ValidationError(
                {"photos": f"At most {cfg['MAX_PHOTOS_PER_REPORT']} photos per report."}
            )

        processed = []
        for i, up in enumerate(data.get("photos", [])):
            try:
                processed.append(process_upload(up, name_hint=up.name))
            except ValueError as e:
                raise serializers.ValidationError({"photos": f"Photo {i + 1}: {e}"}) from e
        data["_processed"] = processed

        lat, lng = data.get("lat"), data.get("lng")
        if lat is None or lng is None:
            hint = next((p.gps for p in processed if p.gps), None)
            if not hint:
                raise serializers.ValidationError(
                    "Provide a location (lat/lng) or a photo with GPS metadata."
                )
            lat, lng = hint
            data["lat"], data["lng"] = lat, lng
            data["_location_from_photo"] = True

        west, south, east, north = cfg["BBOX"]
        if not (west <= lng <= east and south <= lat <= north):
            raise serializers.ValidationError("Location is outside the New Orleans area.")

        if not any([data.get("cross_street"), data.get("street_address"), data.get("host_business")]):
            raise serializers.ValidationError("Give at least one of cross street, address, or host business.")
        return data

    def create(self, validated):
        notes = validated["reporter_notes"]
        if validated.get("_location_from_photo"):
            notes = (notes + "\n\n[location taken from photo GPS]").strip()
        cam = Camera.objects.create(
            location=Point(validated["lng"], validated["lat"], srid=4326),
            camera_type=validated["camera_type"],
            host_business=validated["host_business"],
            cross_street=validated["cross_street"],
            street_address=validated["street_address"],
            facial_recognition=validated["facial_recognition"],
            direction=validated.get("direction"),
            mount=validated.get("mount", ""),
            reporter_contact=validated["reporter_contact"],
            reporter_notes=notes,
            source=CameraSource.FIELD_REPORT,
            status=CameraStatus.PENDING,
        )
        types = validated.get("photo_types") or []
        for i, p in enumerate(validated["_processed"]):
            ptype = types[i] if i < len(types) else PhotoType.CLOSE_UP
            CameraPhoto.objects.create(
                camera=cam,
                image=p.file,
                photo_type=ptype,
                submitted_by=validated["reporter_contact"],
            )
        return cam
