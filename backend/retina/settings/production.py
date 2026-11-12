from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import SECRET_KEY, env, env_bool

DEBUG = env_bool("DJANGO_DEBUG", False)

# The key signs sessions and CSRF tokens; a guessable one lets anyone forge an admin session.
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production")

# Behind CloudFront -> ALB (HTTP). Trust the forwarded proto so Django builds https URLs.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = False  # CloudFront enforces HTTPS; the ALB health check is plain HTTP.

# Throttles key on the client IP. CloudFront and the ALB each append one address
# to X-Forwarded-For, so the client is second from the right; anything further
# left is whatever the sender typed and must not pick the throttle bucket.
REST_FRAMEWORK["NUM_PROXIES"] = int(env("NUM_PROXIES", "2"))  # noqa: F405

# Photos go to S3 and are served through CloudFront at MEDIA_URL.
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
if AWS_STORAGE_BUCKET_NAME:
    STORAGES["default"] = {  # noqa: F405
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            # CloudFront forwards the full /media/<key> path to the bucket.
            "location": "media",
            "default_acl": None,
            "querystring_auth": False,
            "file_overwrite": False,
            "object_parameters": {"CacheControl": "public, max-age=31536000, immutable"},
            # Public URL is the CloudFront /media/* behaviour, not the bucket.
            "custom_domain": env("MEDIA_DOMAIN", ""),
            "url_protocol": "https:",
        },
    }
