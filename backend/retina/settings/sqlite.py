"""
Run the backend with no Postgres at all (SpatiaLite file DB). Handy for a
quick look or for working on the frontend. Not for production.
"""

from .development import *  # noqa: F401,F403
from .development import BASE_DIR

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.spatialite",
        "NAME": BASE_DIR / "dev.sqlite3",
    }
}
