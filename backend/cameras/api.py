import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .geojson import build_feature_collection, camera_to_feature, vetted_queryset
from .models import CameraType
from .serializers import ReportSerializer

log = logging.getLogger(__name__)


def _bool(v):
    return str(v).lower() in ("1", "true", "yes")


class CameraCollection(APIView):
    """
    GET /api/cameras/  -> GeoJSON FeatureCollection of vetted cameras.
    Filters: camera_type=a,b  facial_recognition=true  has_photos=true  bbox=w,s,e,n
    The published static file at /data/cameras.geojson is the same thing,
    cached; this endpoint is for filtered or always-fresh reads.
    """

    authentication_classes = []
    permission_classes = []
    throttle_classes = [AnonRateThrottle]

    def get(self, request):
        qs = vetted_queryset()
        p = request.query_params
        if p.get("camera_type"):
            types = [t for t in p["camera_type"].split(",") if t in CameraType.values]
            qs = qs.filter(camera_type__in=types)
        if p.get("facial_recognition") is not None:
            qs = qs.filter(facial_recognition=_bool(p["facial_recognition"]))
        if p.get("has_photos") is not None:
            if _bool(p["has_photos"]):
                qs = qs.filter(photos__status="approved").distinct()
            else:
                qs = qs.exclude(photos__status="approved")
        if p.get("bbox"):
            try:
                w, s, e, n = (float(x) for x in p["bbox"].split(","))
            except ValueError:
                return Response({"detail": "bbox must be w,s,e,n"}, status=400)
            from django.contrib.gis.geos import Polygon

            qs = qs.filter(location__intersects=Polygon.from_bbox((w, s, e, n)))
        return JsonResponse(build_feature_collection(qs), content_type="application/geo+json")


class CameraDetail(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [AnonRateThrottle]

    def get(self, request, pk):
        cam = get_object_or_404(vetted_queryset(), pk=pk)
        return JsonResponse(camera_to_feature(cam), content_type="application/geo+json")


class ReportThrottle(AnonRateThrottle):
    scope = "report"


class ReportCreate(APIView):
    """
    POST /api/reports/  (multipart or JSON)
    Creates a PENDING camera plus pending photos. Nothing is public until an
    admin vets it. No auth, so no CSRF; abuse is limited by throttling, the
    honeypot field, the bbox check, and image re-encoding.
    """

    authentication_classes = []
    permission_classes = []
    throttle_classes = [ReportThrottle]

    def post(self, request):
        ser = ReportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cam = ser.save()
        log.info("report received camera=%s photos=%d", cam.id, cam.photos.count())
        return Response(
            {"id": str(cam.id), "status": cam.status, "photos": cam.photos.count()},
            status=status.HTTP_201_CREATED,
        )
