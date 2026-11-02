"""
Answer /healthz/ before anything inspects the Host header. The ALB health
check hits the task by IP, which would fail Django's ALLOWED_HOSTS validation.
"""

from django.db import connection
from django.http import JsonResponse


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz/":
            try:
                with connection.cursor() as c:
                    c.execute("SELECT 1")
                return JsonResponse({"status": "ok", "db": "ok"})
            except Exception as e:
                return JsonResponse(
                    {"status": "degraded", "db": f"error: {e.__class__.__name__}"}, status=503
                )
        return self.get_response(request)
