# Django + GeoDjango on arm64. Runs migrations on start, then gunicorn.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# HTTPS mirrors: some networks block plain HTTP from build containers.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin libgdal32 libgeos-c1v5 libproj25 binutils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN DJANGO_SETTINGS_MODULE=retina.settings.production DJANGO_SECRET_KEY=build \
    python manage.py collectstatic --noinput

# -d /app: gunicorn 23 writes its control socket under $HOME
RUN useradd -r -u 10001 -d /app app && chown -R app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8000/healthz/ || exit 1
ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "retina.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-"]
