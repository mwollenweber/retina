"""
Base settings for Retina. Environment-specific modules override what they must.

Configuration comes from environment variables so the same image runs locally,
in docker compose, and on Fargate.
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# No fallback here: development and test supply their own, production refuses to start without one.
SECRET_KEY = env("DJANGO_SECRET_KEY", "")
DEBUG = False
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "rest_framework",
    "cameras",
]

MIDDLEWARE = [
    "cameras.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "retina.urls"
WSGI_APPLICATION = "retina.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --- Database -------------------------------------------------------------
# Resolution order:
#   1. DATABASE_URL  (postgis://user:pass@host:5432/dbname)
#   2. DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD (what the Fargate task gets)
#   3. DATABASE_SECRET_ARN (fetch RDS secret JSON from Secrets Manager)
def _db_from_env():
    url = env("DATABASE_URL")
    if url:
        import dj_database_url

        cfg = dj_database_url.parse(url)
        cfg["ENGINE"] = "django.contrib.gis.db.backends.postgis"
        return cfg

    if env("DB_HOST"):
        return {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "HOST": env("DB_HOST"),
            "PORT": env("DB_PORT", "5432"),
            "NAME": env("DB_NAME", "retina"),
            "USER": env("DB_USER", "retina"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "CONN_MAX_AGE": 60,
        }

    arn = env("DATABASE_SECRET_ARN")
    if arn:
        import boto3

        raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
        s = json.loads(raw)
        return {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "HOST": s["host"],
            "PORT": str(s.get("port", 5432)),
            "NAME": s.get("dbname", "retina"),
            "USER": s["username"],
            "PASSWORD": s["password"],
            "CONN_MAX_AGE": 60,
        }

    return None


_db = _db_from_env()
DATABASES = {"default": _db} if _db else {}

# --- GeoDjango native libs (macOS Homebrew paths are auto-detected) ---------
for _name, _candidates in {
    "GDAL_LIBRARY_PATH": [
        "/opt/homebrew/opt/gdal/lib/libgdal.dylib",
        "/usr/local/opt/gdal/lib/libgdal.dylib",
    ],
    "GEOS_LIBRARY_PATH": [
        "/opt/homebrew/opt/geos/lib/libgeos_c.dylib",
        "/usr/local/opt/geos/lib/libgeos_c.dylib",
    ],
}.items():
    _val = env(_name)
    if not _val:
        for _c in _candidates:
            if os.path.exists(_c):
                _val = _c
                break
    if _val:
        globals()[_name] = _val

# SpatiaLite (used by the test and sqlite settings); harmless under PostGIS.
for _c in [
    env("SPATIALITE_LIBRARY_PATH", ""),
    "/opt/homebrew/lib/mod_spatialite.dylib",
    "/usr/local/lib/mod_spatialite.dylib",
    "/usr/lib/x86_64-linux-gnu/mod_spatialite.so",
    "/usr/lib/aarch64-linux-gnu/mod_spatialite.so",
]:
    if _c and os.path.exists(_c):
        SPATIALITE_LIBRARY_PATH = _c
        break
else:
    SPATIALITE_LIBRARY_PATH = "mod_spatialite"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = env("MEDIA_URL", "/media/")
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Uploads: Django will refuse bigger multipart bodies.
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "120/min",
        "report": env("REPORT_THROTTLE_RATE", "10/hour"),
    },
    "UNAUTHENTICATED_USER": None,
}

# --- Retina-specific ------------------------------------------------------
RETINA = {
    # Max photo dimension on the long edge after re-encoding (EXIF is stripped).
    "PHOTO_MAX_EDGE": int(env("PHOTO_MAX_EDGE", "2000")),
    "PHOTO_JPEG_QUALITY": int(env("PHOTO_JPEG_QUALITY", "85")),
    "MAX_PHOTOS_PER_REPORT": int(env("MAX_PHOTOS_PER_REPORT", "4")),
    # Where publish_geojson writes. If DATA_BUCKET is unset it writes a local file.
    "DATA_BUCKET": env("DATA_BUCKET"),
    "DATA_KEY": env("DATA_KEY", "data/cameras.geojson"),
    "LOCAL_GEOJSON_PATH": env(
        "LOCAL_GEOJSON_PATH", str(BASE_DIR.parent / "frontend" / "data" / "cameras.geojson")
    ),
    "CLOUDFRONT_DISTRIBUTION_ID": env("CLOUDFRONT_DISTRIBUTION_ID"),
    # If set, the admin "publish" action invokes this Lambda instead of publishing inline.
    "PUBLISH_FUNCTION_NAME": env("PUBLISH_FUNCTION_NAME"),
    # Bounding box used to reject obviously wrong submissions (greater New Orleans).
    "BBOX": (-90.30, 29.80, -89.60, 30.20),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
