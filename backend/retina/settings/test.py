"""
Test settings: SpatiaLite so the suite runs without Postgres.
PostGIS-only features (geography type) degrade to plain geometry here.
"""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR

DEBUG = False
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.spatialite",
        "NAME": ":memory:",
    }
}

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MEDIA_ROOT = BASE_DIR / "test-media"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["report"] = "1000/hour"  # noqa: F405
RETINA["LOCAL_GEOJSON_PATH"] = str(BASE_DIR / "test-media" / "cameras.geojson")  # noqa: F405

# WhiteNoise warns when STATIC_ROOT does not exist; not needed under test.
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m]  # noqa: F405
