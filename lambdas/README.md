# Retina scheduled jobs (Lambdas)

Four small Python 3.13 / arm64 Lambdas that keep the camera table and the
published map data fresh. They talk to Postgres directly with psycopg 3 (no
Django, no GDAL, no shapely) and write to the tables the Django app owns:
`camera`, `camera_photo`, `source_sync`. **`common/schema.py` must mirror
`backend/cameras/models.py`.**

Every run inserts a `source_sync` row (`status=running`) at start and updates it
(`succeeded` / `failed`, counts, error text, `finished_at`) at the end, so the
admin can see the job history.

```
lambdas/
  common/           db.py (connection), sync_log.py (source_sync context manager),
                    cameras.py (INSERT/UPDATE helpers), schema.py (table + choice constants)
  sync_traffic/     data.nola.gov traffic cameras -> camera
  sync_osm/         OpenStreetMap surveillance nodes (Overpass) -> camera
  publish_geojson/  vetted cameras + approved photos -> S3 GeoJSON
  push_osm/         field-surveyed cameras -> OpenStreetMap (dry run by default)
  tests/            pytest; no network, fake DB, moto for S3
  build.sh          one zip per handler in build/<name>.zip
```

## Jobs

### `sync_traffic` (`sync_traffic.handler.handler`)
Pages through the City's Socrata dataset `te2d-4txp` (`$limit=1000&$offset=`).
Each record with a `the_geom` point and a `camid` is upserted on
`(source='city_traffic', source_ref=camid)`:

* insert: `camera_type='traffic'`, `status='vetted'` (official data),
  `operator='City of New Orleans'`, `street_address=camloc`, `direction` from
  `dir` (`NB`→0, `EB`→90, `SB`→180, `WB`→270), `reported_at`/`vetted_at`=now,
  `admin_notes` = one line with function / cat / school / install / operational / active.
* update: same columns refreshed and `updated_at` bumped; **`status`,
  `reported_at`, `vetted_at` are never touched**, so an admin rejection sticks.
* Rows with `active='No'` are kept and flagged `INACTIVE per city data` in `admin_notes`.
* Rows without geometry are counted as seen and skipped.

### `sync_osm` (`sync_osm.handler.handler`)
POSTs an Overpass query for `node["man_made"="surveillance"]` inside Orleans
Parish (area `3601836428` = relation 1836428 + 3600000000) with the
`User-Agent: retina-nola-camera-map/0.1 (+https://github.com/mwollenweber/retina)`
header (Overpass returns 406 without one). Each node is upserted on `osm_id`:

* `source='osm'`, `source_ref=str(osm_id)`, `status='vetted'` on insert.
* `camera_type`: `surveillance:type=ALPR` → `alpr`; operator/brand/name containing
  "project nola" → `project_nola`; operator containing NOPD / New Orleans Police → `nopd`;
  `surveillance=traffic` → `traffic`; else `unknown`.
* `manufacturer`, `operator`, `direction` (`camera:direction` or `direction`, integer
  0–359 or a compass abbreviation; ranges like `90-180` ignored), `mount`
  (`camera:mount` → pole / building / traffic_signal / other), `facial_recognition`
  (`surveillance:type` in fr/face/facial), `street_address` from `addr:*`,
  `admin_notes` = JSON of all tags.
* **Only rows with `source='osm'` are updated**, and `status` is never changed on
  update. A camera that came from a field report and was pushed to OSM keeps its
  own data.

### `publish_geojson` (`publish_geojson.handler.handler`)
Selects cameras with `status='vetted'` and their `status='approved'` photos,
builds a GeoJSON `FeatureCollection` (`generated_at`, `count`, `features`), and
uploads it to `s3://$DATA_BUCKET/$DATA_KEY` with
`Content-Type: application/geo+json` and `Cache-Control: public, max-age=300`.
Feature `id` is the camera uuid; properties are `id, camera_type, operator,
manufacturer, model_name, facial_recognition, direction, mount, cross_street,
street_address, host_business, source, osm_id, vetted_at, photos[{url,type,caption}]`.
Photo `url` = `$PHOTO_BASE_URL/<camera_photo.image>`.
`reporter_contact`, `reporter_notes`, `admin_notes` and `submitted_by` are never
selected and the job refuses to publish if they appear. If
`CLOUDFRONT_DISTRIBUTION_ID` is set it also invalidates `/$DATA_KEY`.
Set `LOCAL_OUTPUT_PATH` to write a file instead of S3.

### `push_osm` (`push_osm.handler.handler`)
Creates OSM nodes for cameras with `status='vetted'`, `source IN
('field_report','admin')`, `osm_id IS NULL` and `camera_type` in
`OSM_PUSH_TYPES` (default `project_nola`). One changeset per run
(`created_by=retina`, comment "Add surveillance cameras in New Orleans, surveyed
in the field"), one node per camera with tags `man_made=surveillance`,
`surveillance=outdoor`, `surveillance:type=camera`, `camera:type=fixed`,
`operator`, `source=survey`, plus `manufacturer`, `camera:direction`,
`camera:mount` when present. The new node id is written back to
`camera.osm_id` / `osm_pushed_at` and committed per node.

**Dry run by default.** Nothing is sent unless `OSM_PUSH_ENABLED=true`; the dry
run logs and returns the planned nodes. Capped at `OSM_PUSH_MAX` (25) per run.
OSM's import guidelines expect community discussion before bulk uploads, so
keep this small, opt-in, and try the sandbox first
(`OSM_API_URL=https://master.apis.dev.openstreetmap.org/api/0.6`).

## Environment variables

| Variable | Used by | Default | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | all | – | `postgres://user:pass@host:5432/db`. Wins over the secret. |
| `DB_SECRET_ARN` | all | – | Secrets Manager secret with RDS JSON (`username, password, host, port, dbname`). Cached per execution environment. |
| `DB_SSLMODE` | all | `require` | Only applied when building the DSN from the secret. |
| `DB_CONNECT_TIMEOUT` | all | `10` | seconds |
| `LOG_LEVEL` | all | `INFO` | |
| `SOCRATA_URL` | sync_traffic | `https://data.nola.gov/resource/te2d-4txp.json` | |
| `SOCRATA_APP_TOKEN` | sync_traffic | – | Sent as `X-App-Token`; raises Socrata rate limits. |
| `OVERPASS_URL` | sync_osm | `https://overpass-api.de/api/interpreter` | |
| `DATA_BUCKET` | publish_geojson | – | Required unless `LOCAL_OUTPUT_PATH` is set. |
| `DATA_KEY` | publish_geojson | `data/cameras.geojson` | |
| `PHOTO_BASE_URL` | publish_geojson | `` | e.g. `https://d123.cloudfront.net` (no trailing slash). |
| `CLOUDFRONT_DISTRIBUTION_ID` | publish_geojson | – | Invalidate `/$DATA_KEY` after upload. |
| `LOCAL_OUTPUT_PATH` | publish_geojson | – | Write to this file instead of S3 (local dev). |
| `OSM_API_URL` | push_osm | `https://api.openstreetmap.org/api/0.6` | Use `https://master.apis.dev.openstreetmap.org/api/0.6` for the sandbox. |
| `OSM_ACCESS_TOKEN` | push_osm | – | OAuth 2 bearer token with `write_api` scope. |
| `OSM_PUSH_ENABLED` | push_osm | `false` | Must be literally `true` to write. |
| `OSM_PUSH_MAX` | push_osm | `25` | Nodes per run. |
| `OSM_PUSH_TYPES` | push_osm | `project_nola` | Comma list of `camera_type` values to push. |

## Running locally

```bash
python3.14 -m venv ~/.venvs/retina-lambdas     # keep venvs out of the iCloud-synced repo
~/.venvs/retina-lambdas/bin/pip install -r requirements-dev.txt
cd lambdas
export PYTHONPATH=.

# tests (no network, no database)
~/.venvs/retina-lambdas/bin/pytest

# run a job against a local Postgres/PostGIS
DATABASE_URL=postgres://retina:retina@localhost:5432/retina \
  ~/.venvs/retina-lambdas/bin/python -c "from sync_traffic.handler import handler; print(handler({}, None))"

DATABASE_URL=... ~/.venvs/retina-lambdas/bin/python -c "from sync_osm.handler import handler; print(handler({}, None))"

DATABASE_URL=... LOCAL_OUTPUT_PATH=../frontend/data/cameras.geojson PHOTO_BASE_URL=http://localhost:8000/media \
  ~/.venvs/retina-lambdas/bin/python -c "from publish_geojson.handler import handler; print(handler({}, None))"

# dry run (default) against the OSM sandbox
DATABASE_URL=... OSM_API_URL=https://master.apis.dev.openstreetmap.org/api/0.6 \
  ~/.venvs/retina-lambdas/bin/python -c "from push_osm.handler import handler; print(handler({}, None))"
```

## Building

```bash
cd lambdas
PYTHON=~/.venvs/retina-lambdas/bin/python ./build.sh
# -> build/sync_traffic.zip build/sync_osm.zip build/publish_geojson.zip build/push_osm.zip
```

`build.sh` resolves `psycopg[binary]` and `requests` with
`pip install --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.13`
so the zips contain arm64 wheels (psycopg-binary 3.3+ only ships
`manylinux_2_28` for aarch64; the python3.13 Lambda runtime is Amazon Linux 2023
with glibc 2.34, so that is fine), then adds `common/` and the handler package.
`boto3` is left out because the Lambda runtime provides it. CDK points each
function at `build/<name>.zip` with handler `<name>.handler.handler`, runtime
`python3.13`, architecture `arm64`.

## Schedules

| Job | Schedule | Why |
| --- | --- | --- |
| `sync_traffic` | daily (e.g. 06:00 UTC) | The city dataset changes rarely. |
| `sync_osm` | every 6 hours | Overpass is a shared public service; be polite. |
| `publish_geojson` | every 15 minutes, **and** after admin approve/reject actions | Cheap; 15 min bounds staleness; the backend can invoke it on demand (`lambda:InvokeFunction`). |
| `push_osm` | manual, or daily in dry-run mode | Review the dry-run output, then run with `OSM_PUSH_ENABLED=true` by hand. |

Suggested Lambda settings: 256 MB, 2 minute timeout (5 for `sync_osm`, Overpass
can be slow), reserved concurrency 1 per function so runs never overlap.
