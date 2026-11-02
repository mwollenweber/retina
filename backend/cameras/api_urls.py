from django.urls import path

from . import api

urlpatterns = [
    path("cameras/", api.CameraCollection.as_view(), name="api-cameras"),
    path("cameras/<uuid:pk>/", api.CameraDetail.as_view(), name="api-camera"),
    path("reports/", api.ReportCreate.as_view(), name="api-report"),
]
