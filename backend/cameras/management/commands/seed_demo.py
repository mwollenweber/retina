"""
Creates a few clearly-labelled DEMO cameras so the map has something to show
locally. These are not real camera locations. Never run in production.
"""

from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cameras.models import Camera, CameraSource, CameraStatus, CameraType

DEMO = [
    (-90.0715, 29.9511, CameraType.PROJECT_NOLA, "Canal St & Bourbon St", True),
    (-90.0650, 29.9580, CameraType.PROJECT_NOLA, "Esplanade Ave & Decatur St", False),
    (-90.0800, 29.9450, CameraType.NOPD, "Poydras St & Loyola Ave", False),
    (-90.1000, 29.9400, CameraType.ALPR, "S Claiborne Ave & Napoleon Ave", False),
    (-90.0900, 29.9700, CameraType.TRAFFIC, "N Broad St & Orleans Ave", False),
]


class Command(BaseCommand):
    help = "Insert demo cameras (local development only)."

    def handle(self, *args, **options):
        if not settings.DEBUG and "test" not in settings.SETTINGS_MODULE:
            raise CommandError("Refusing to seed demo data outside DEBUG.")
        n = 0
        for lng, lat, ctype, cross, fr in DEMO:
            _, created = Camera.objects.get_or_create(
                source=CameraSource.ADMIN,
                source_ref=f"demo-{cross}",
                defaults=dict(
                    location=Point(lng, lat, srid=4326),
                    camera_type=ctype,
                    cross_street=cross,
                    host_business="DEMO DATA",
                    facial_recognition=fr,
                    status=CameraStatus.VETTED,
                    vetted_at=timezone.now(),
                    admin_notes="Synthetic demo record. Not a real camera.",
                ),
            )
            n += created
        self.stdout.write(f"created {n} demo cameras")
