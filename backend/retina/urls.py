from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from cameras.views import healthz

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("api/", include("cameras.api_urls")),
]

# Local uploads. In production MEDIA_URL points at CloudFront, so this is a no-op there.
if settings.DEBUG and not str(settings.MEDIA_URL).startswith("http"):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
