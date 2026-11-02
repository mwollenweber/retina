"""
Import cameras from a spreadsheet of names and street addresses.

Expected columns (header row, extra columns ignored):
    Camera Name/Nearest Business, Apparent Address, Notes

Each row is geocoded (Census, then Nominatim) and upserted as a camera with
source=admin and a stable source_ref derived from the geocoder's canonical
address, so
re-running the command updates rather than duplicates. The sheet is the
owner's own field data, so every geocoded row is published (vetted). Anything
in Notes (e.g. "owner denies Project NOLA") and the geocoder used are kept in
admin_notes for review in the admin; --pending holds everything back instead.
Rows without an address, or that neither geocoder can place, are reported and
skipped.

Usage:
    manage.py import_sheet --url 'https://docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=0'
    manage.py import_sheet --file cameras.csv --camera-type project_nola --dry-run
"""

import csv
import io
import re
import urllib.request
from collections import OrderedDict

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cameras.geocode import USER_AGENT, geocode, normalize_address
from cameras.models import Camera, CameraSource, CameraStatus, CameraType
from cameras.publish import publish


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def read_rows(text: str) -> list[dict]:
    """Parse the CSV into dicts with name/address/notes, dropping blank rows."""
    reader = csv.reader(io.StringIO(text))
    rows = []
    for i, cells in enumerate(reader):
        if i == 0:
            continue  # header
        cells = [c.strip() for c in cells] + ["", "", ""]
        name, address, notes = cells[0], cells[1], cells[2]
        if not (name or address):
            continue
        rows.append({"row": i + 1, "name": name, "address": address, "notes": notes})
    return rows


def group_by_address(rows: list[dict]) -> "OrderedDict[str, dict]":
    """Merge rows that share a normalized address; any note taints the group."""
    groups: OrderedDict[str, dict] = OrderedDict()
    for r in rows:
        key = slugify(normalize_address(r["address"]))
        g = groups.setdefault(key, {"address": r["address"], "names": [], "notes": [], "rows": []})
        if r["name"] and r["name"] not in g["names"]:
            g["names"].append(r["name"])
        if r["notes"]:
            g["notes"].append(r["notes"])
        g["rows"].append(r["row"])
    return groups


class Command(BaseCommand):
    help = "Import cameras from a CSV of names and addresses (geocoded on the fly)."

    def add_arguments(self, parser):
        src = parser.add_mutually_exclusive_group(required=True)
        src.add_argument("--url", help="CSV URL, e.g. a Google Sheets export link")
        src.add_argument("--file", help="Local CSV path")
        parser.add_argument(
            "--camera-type", default=CameraType.PROJECT_NOLA, choices=[c.value for c in CameraType]
        )
        parser.add_argument(
            "--source-ref-prefix", default="gsheet", help="Prefix for the stable source_ref (default: gsheet)"
        )
        parser.add_argument(
            "--pending", action="store_true", help="Import everything as pending instead of vetted"
        )
        parser.add_argument("--dry-run", action="store_true", help="Geocode and report only")
        parser.add_argument("--publish", action="store_true", help="Republish the GeoJSON after importing")

    def handle(self, *args, **opts):
        text = self._load(opts)
        rows = read_rows(text)
        groups = group_by_address(rows)
        self.stdout.write(f"{len(rows)} rows, {len(groups)} distinct addresses")

        now = timezone.now()
        # Geocode each distinct input address, then merge groups whose geocoder
        # result is the same place ("4218 Magazine" and "4218 Magazine St").
        resolved: OrderedDict[str, dict] = OrderedDict()
        skipped: list[str] = []
        for g in groups.values():
            label = " / ".join(g["names"]) or "(unnamed)"
            if not g["address"]:
                skipped.append(f"rows {g['rows']}: {label}: no address")
                continue
            hit = geocode(g["address"])
            if hit is None:
                skipped.append(f"rows {g['rows']}: {label}: could not geocode {g['address']!r}")
                continue
            r = resolved.setdefault(
                slugify(hit.matched_address),
                {"hit": hit, "names": [], "notes": [], "rows": [], "addresses": []},
            )
            r["names"] += [n for n in g["names"] if n not in r["names"]]
            r["notes"] += g["notes"]
            r["rows"] += g["rows"]
            r["addresses"].append(g["address"])

        created = updated = pending = 0
        for key, r in resolved.items():
            hit = r["hit"]
            label = " / ".join(r["names"]) or "(unnamed)"
            notes = list(r["notes"])
            if hit.geocoder != "census":
                notes.append(f"Location from {hit.geocoder} fallback geocoder; worth a Street View check")
            is_pending = opts["pending"]
            status = CameraStatus.PENDING if is_pending else CameraStatus.VETTED
            note_text = "; ".join(notes)
            self.stdout.write(
                f"  {'PENDING' if is_pending else 'vetted '} {label:<40.40} "
                f"{r['addresses'][0]:<32.32} -> {hit.lat:.5f},{hit.lng:.5f} ({hit.geocoder})"
                + (f"  [{note_text}]" if note_text else "")
            )
            pending += is_pending
            if opts["dry_run"]:
                continue

            defaults = dict(
                location=Point(hit.lng, hit.lat, srid=4326),
                camera_type=opts["camera_type"],
                host_business=label[:255],
                street_address=hit.matched_address[:255],
                status=status,
                vetted_at=None if is_pending else now,
                admin_notes=(
                    f"Imported from spreadsheet rows {r['rows']}; "
                    f"geocoded {r['addresses']!r} with {hit.geocoder}."
                    + (f" Sheet notes: {note_text}" if note_text else "")
                ),
            )
            _, was_created = Camera.objects.update_or_create(
                source=CameraSource.ADMIN,
                source_ref=f"{opts['source_ref_prefix']}:{key}"[:255],
                defaults=defaults,
            )
            created += was_created
            updated += not was_created

        for line in skipped:
            self.stdout.write(self.style.WARNING(f"  skipped {line}"))
        verb = "would import" if opts["dry_run"] else "imported"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb}: {created} created, {updated} updated, {pending} pending, {len(skipped)} skipped"
            )
        )
        if opts["publish"] and not opts["dry_run"]:
            self.stdout.write(publish())

    @staticmethod
    def _load(opts) -> str:
        if opts["file"]:
            with open(opts["file"], encoding="utf-8-sig") as fh:
                return fh.read()
        url = opts["url"]
        if not url.startswith("https://"):
            raise CommandError("--url must be https")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.read().decode("utf-8-sig")
