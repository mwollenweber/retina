from django.core.management.base import BaseCommand

from cameras.publish import publish_inline


class Command(BaseCommand):
    help = "Build the vetted-camera GeoJSON and write it to S3 (DATA_BUCKET) or the local frontend/data path."

    def handle(self, *args, **options):
        self.stdout.write(publish_inline())
