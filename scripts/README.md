# Keeping the map data fresh

Every source refreshes itself on a schedule. The scripts here are for running
a refresh on demand, re-importing the spreadsheet after you edit it, or
checking what a job did. All of them use the AWS CLI with the default profile.

## What updates automatically

| Source | What it is | Job | Schedule (UTC) | Camera type / source |
|---|---|---|---|---|
| NOPD / RTCC | City "Public Safety Cameras" ArcGIS layer (`RTCC_Cameras`) behind nolagis.maps.arcgis.com | `sync_rtcc` Lambda | daily 06:30 | `nopd` / `city_rtcc` |
| Flock and other plate readers | OpenStreetMap `man_made=surveillance` nodes within 10 miles of downtown, via Overpass. This is the same data flockcameralocations.com and DeFlock draw from. | `sync_osm` Lambda | every 6 hours | mostly `alpr` / `osm` |
| City traffic cameras | data.nola.gov dataset `te2d-4txp` | `sync_traffic` Lambda | daily 06:00 | `traffic` / `city_traffic` |
| Project NOLA sheet | The Google Sheet of names and addresses, geocoded (Census, then Nominatim) | `import_sheet` as a one-off ECS task | daily 05:30 | `project_nola` / `admin` |
| Published map | `data/cameras.geojson` rebuilt from vetted cameras, CloudFront invalidated | `publish_geojson` Lambda | every 15 minutes, and right after the sheet import | |

Rules that apply to every sync:

* A camera is matched on `(source, source_ref)` and updated in place. Nothing is
  ever duplicated by re-running a job.
* Syncs never change `status`. If you reject a camera in the admin, the next sync
  leaves it rejected.
* Every run writes a row to the `source_sync` table (visible in the admin as
  "Source syncs") with counts, timing, and the error text if it failed.

## Run a refresh now

```sh
scripts/sync-now.sh            # everything, then publish
scripts/sync-now.sh rtcc       # just the City RTCC layer, then publish
scripts/sync-now.sh osm        # just OpenStreetMap / Flock, then publish
scripts/sync-now.sh traffic    # just data.nola.gov, then publish
scripts/sync-now.sh sheet      # just the Google Sheet (same as import-sheet.sh)
scripts/sync-now.sh publish    # just rebuild the GeoJSON
```

The Lambda sources finish in seconds and print their counts. The sheet import
launches a Fargate task, waits for it, and prints the per-row log; it takes a
few minutes because each address is geocoded.

## After editing the Google Sheet

```sh
scripts/import-sheet.sh            # import and publish
scripts/import-sheet.sh --dry-run  # geocode and report, write nothing
```

Rows are matched by their geocoded address, so renaming a business or fixing a
typo in an address updates the existing camera instead of adding another. Rows
with no address, or an address neither geocoder can place, are listed at the
end of the log as "skipped". Fix them in the sheet and re-run.

If a row can't be fixed in the sheet (a camera name with no address, say),
add it to `backend/cameras/data/sheet_fixups.csv` with the address you looked
up. Fixups apply by row name and leave the sheet alone. They ship in the
backend image, so a fixup change needs `cdk deploy RetinaApp` before it takes
effect.

The sheet URL lives in `infra/deploy.local.json` as `sheetCsvUrl`, which is not
in git (see `infra/deploy.example.json`). The schedule and `import-sheet.sh`
both read it from there; `SHEET_URL` overrides it for a manual run. If the
sheet moves, change it and `cdk deploy RetinaApp`.

## Checking on a job

* Admin, "Source syncs": one row per run with counts and errors.
* CloudWatch Logs: `/aws/lambda/RetinaJobs-SyncRtcc…` and friends for the
  Lambdas; the `RetinaApp-WebLogs…` group, stream prefix `web/web/`, for sheet
  imports.
* The Overpass API returns 502 from time to time. `sync_osm` just fails that
  run and succeeds on the next one six hours later.

## Changing a schedule

Lambda schedules are in `infra/stacks/jobs.py`; the sheet import schedule is
in `infra/stacks/web.py`. Both are EventBridge cron expressions in UTC. Deploy
the matching stack after editing.
