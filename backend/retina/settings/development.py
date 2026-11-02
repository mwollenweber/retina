from .base import *  # noqa: F401,F403
from .base import BASE_DIR, DATABASES, env

DEBUG = True
ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:5173"]

if not DATABASES:
    # docker compose default
    DATABASES = {
        "default": {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "HOST": env("DB_HOST", "localhost"),
            "PORT": env("DB_PORT", "5432"),
            "NAME": env("DB_NAME", "retina"),
            "USER": env("DB_USER", "retina"),
            "PASSWORD": env("DB_PASSWORD", "retina"),
        }
    }

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

# Let the static frontend (served from another origin in dev) call the API.
INSTALLED_APPS += ["corsheaders"]  # noqa: F405
MIDDLEWARE.insert(1, "corsheaders.middleware.CorsMiddleware")  # noqa: F405
CORS_ALLOW_ALL_ORIGINS = True
