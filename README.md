# Retina

A project of the [Perdido Institute](https://perdidoinstitute.com): a self-hosted,
interactive map of surveillance cameras in New Orleans, with a focus on Project NOLA.
Contact: retina@perdidoinstitute.com. Cameras are collected in the field by the public, reviewed by an admin with
photos, and published to a static map. Scheduled jobs pull official city traffic cameras and
OpenStreetMap data, and can push field-verified cameras back to OpenStreetMap.

```
frontend/   Static map + report form (MapLibre GL, no build step)        -> S3 + CloudFront
backend/    Django 5/6 + GeoDjango + DRF: API, admin review queue, photos -> ECS Fargate
lambdas/    Python 3.13 scheduled jobs (sync city data, sync OSM, publish GeoJSON, push OSM)
infra/      AWS CDK (Python): VPC, RDS PostGIS, S3, CloudFront, ALB, Fargate, Lambdas, schedules
```

## How data flows

1. Someone spots a camera and files a report at `/submit.html` with a pin, host business, and
   photos. The API stores it as **pending**. Photos are re-encoded and all EXIF (GPS, device
   serial) is stripped before they reach storage. If the reporter gave no pin, GPS from the
   photo is used as the location hint before being discarded.
2. An admin reviews it at `/admin/`, approves or rejects the camera and each photo.
   Reporter contact and notes are never published.
3. `publish_geojson` (Lambda every 15 min, or the admin "Publish map data now" action) writes
   `data/cameras.geojson` of vetted cameras to S3. The map reads that file.
4. `sync_traffic` (daily) upserts the City's traffic cameras from data.nola.gov.
   The Project NOLA Google Sheet is re-imported daily by a scheduled ECS task.
   See `scripts/README.md` for schedules and how to refresh any source by hand.
   `sync_rtcc` (daily) upserts the City's public safety (RTCC) cameras from its ArcGIS feature service.
   `sync_osm` (every 6 h) upserts `man_made=surveillance` nodes within 10 miles of downtown.
   Neither job ever changes a camera's status after insert, so admin decisions stick.
5. `push_osm` (daily, **dry run by default**) creates OSM nodes for vetted, field-reported
   Project NOLA cameras and records the node id back.

Data sources are: public field reports, data.nola.gov, OpenStreetMap, and public records.
Nothing is pulled from other camera-mapping sites.

## Local development

### Quickest: no Postgres

```sh
brew install gdal geos libspatialite          # macOS; Debian: gdal-bin libgdal-dev libgeos-dev libsqlite3-mod-spatialite
python3 -m venv ~/.venvs/retina && . ~/.venvs/retina/bin/activate   # keep venvs out of iCloud
pip install -r backend/requirements-dev.txt
cd backend
export DJANGO_SETTINGS_MODULE=retina.settings.sqlite
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo                    # 5 synthetic cameras, clearly labelled DEMO
python manage.py publish_geojson              # -> frontend/data/cameras.geojson
python manage.py runserver                    # API + admin on :8000
```

In another shell:

```sh
cd frontend && python3 -m http.server 5173     # map on http://localhost:5173
```

Set `API_BASE: "http://localhost:8000"` in `frontend/config.js` so the report form can post.

### With PostGIS (matches production)

```sh
docker compose up        # PostGIS on :5432, Django on :8000 (runs migrations on start)
docker compose exec web python manage.py createsuperuser
```

### Tests

```sh
cd backend && DJANGO_SETTINGS_MODULE=retina.settings.test pytest   # SpatiaLite in memory
cd lambdas && PYTHONPATH=. pytest                                   # no network, fake DB, moto
cd infra && npx aws-cdk@2 synth                                     # no AWS creds needed
```

CI runs all three plus a Docker build (`.github/workflows/ci.yml`).

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/cameras/` | GeoJSON of vetted cameras. Filters: `camera_type=a,b`, `facial_recognition=true`, `has_photos=true`, `bbox=w,s,e,n` |
| GET | `/api/cameras/<uuid>/` | One feature |
| POST | `/api/reports/` | Multipart public report. Throttled (`REPORT_THROTTLE_RATE`, default 10/hour/IP), honeypot field `website`, bbox check |
| GET | `/healthz/` | DB check, used by the ALB |

Camera types: `project_nola`, `nopd`, `traffic`, `alpr`, `private`, `unknown`.

## Deploying

See `infra/README.md`. Short version: build the Lambda zips (`lambdas/build.sh`),
`cdk deploy --all`, run the migrate and createsuperuser one-off tasks, upload `frontend/` to
the site bucket, set the OSM token secret when you are ready to push.

Estimated cost is roughly $45/month: RDS t4g.micro, one Fargate task, an ALB, a t4g.nano NAT
instance. Lambdas, S3, and CloudFront are effectively free at this scale.

## Configuration

Backend env vars (see `backend/.env.example` and `retina/settings/base.py`):
`DATABASE_URL` or `DB_*` or `DATABASE_SECRET_ARN`; `DJANGO_SECRET_KEY`; `ALLOWED_HOSTS`;
`CSRF_TRUSTED_ORIGINS`; `AWS_STORAGE_BUCKET_NAME` + `MEDIA_URL` + `MEDIA_DOMAIN` for S3 photos;
`DATA_BUCKET`/`DATA_KEY`/`CLOUDFRONT_DISTRIBUTION_ID` or `PUBLISH_FUNCTION_NAME` for publishing;
`PHOTO_MAX_EDGE`, `PHOTO_JPEG_QUALITY`, `MAX_PHOTOS_PER_REPORT`, `REPORT_THROTTLE_RATE`.

Lambda env vars are listed in `lambdas/README.md`. Frontend settings are in `frontend/config.js`.

## Field guide for Project NOLA cameras

Project NOLA cameras are usually larger than ordinary business cameras, often on a pan-tilt
mount on the exterior of a cooperating business, pointed at the street rather than the
storefront, sometimes with a Project NOLA sign or sticker on the host business. A close-up
photo plus a wider shot showing the host building is what reviewers need to confirm one.
