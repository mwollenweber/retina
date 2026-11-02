from django.db import connection
from django.http import JsonResponse


def healthz(request):
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
        db = "ok"
        code = 200
    except Exception as e:  # pragma: no cover
        db = f"error: {e.__class__.__name__}"
        code = 503
    return JsonResponse({"status": "ok" if code == 200 else "degraded", "db": db}, status=code)
