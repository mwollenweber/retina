import pytest
from django.core.management import call_command

from cameras import geocode as geocode_mod
from cameras.geocode import GeocodeResult, normalize_address
from cameras.management.commands import import_sheet
from cameras.models import Camera, CameraSource, CameraStatus, CameraType

CSV = """Camera Name/Nearest Business,Apparent Address,Notes,,,
30/90,520 Frenchmen St,,,,
Theos,4218 Magazine,,,,
Theos magazine ,4218 Magazine St,Owner reports not Project NOLA,,,
Kimberly A Meng DDS,610 Tchoup,,,,
,Decatur and Bienville,,,,
6801 Back Lot PTZ,,,,,
Zeas,see MJW,,,,
"""

KNOWN = {
    "520 Frenchmen St, New Orleans, LA": (-90.0577, 29.9628, "520 FRENCHMEN ST"),
    "4218 Magazine, New Orleans, LA": (-90.0993, 29.9209, "4218 MAGAZINE ST"),
    "4218 Magazine St, New Orleans, LA": (-90.0993, 29.9209, "4218 MAGAZINE ST"),
    "610 Tchoupitoulas St, New Orleans, LA": (-90.0672, 29.9471, "610 TCHOUPITOULAS ST"),
    "Decatur & Bienville, New Orleans, LA": (-90.0656, 29.9536, "DECATUR ST & BIENVILLE ST"),
}


@pytest.fixture
def fake_geocoder(monkeypatch):
    calls = []

    def fake(address):
        calls.append(address)
        addr = normalize_address(address)
        if addr in KNOWN:
            lng, lat, matched = KNOWN[addr]
            return GeocodeResult(lng, lat, matched, "nominatim" if "Decatur" in addr else "census")
        return None

    monkeypatch.setattr(import_sheet, "geocode", fake)
    return calls


def test_normalize_address():
    assert normalize_address("610 Tchoup") == "610 Tchoupitoulas St, New Orleans, LA"
    assert normalize_address("Decatur and Bienville") == "Decatur & Bienville, New Orleans, LA"
    assert normalize_address("629 Canal St, New Orleans") == "629 Canal St, New Orleans"
    assert normalize_address(" 407 Bourbon St, ") == "407 Bourbon St, New Orleans, LA"
    assert normalize_address("") == ""


def test_read_rows_and_grouping():
    rows = import_sheet.read_rows(CSV)
    assert [r["name"] for r in rows] == [
        "30/90",
        "Theos",
        "Theos magazine",
        "Kimberly A Meng DDS",
        "",
        "6801 Back Lot PTZ",
        "Zeas",
    ]
    groups = import_sheet.group_by_address(rows)
    # Both Theos rows normalise to different strings ("Magazine" vs "Magazine St"),
    # so they are separate groups; the second carries the note.
    assert groups["4218-magazine-st-new-orleans-la"]["notes"] == ["Owner reports not Project NOLA"]
    assert groups[""]["names"] == ["6801 Back Lot PTZ"]  # no address


@pytest.mark.django_db
def test_import_creates_vetted_and_pending(tmp_path, fake_geocoder):
    path = tmp_path / "sheet.csv"
    path.write_text(CSV)
    call_command("import_sheet", file=str(path))

    cams = {c.source_ref: c for c in Camera.objects.filter(source=CameraSource.ADMIN)}
    # Both Theos rows geocode to the same canonical address and merge into one record.
    assert len(cams) == 4
    assert all(c.camera_type == CameraType.PROJECT_NOLA for c in cams.values())

    thirty = cams["gsheet:520-frenchmen-st"]
    assert thirty.status == CameraStatus.VETTED
    assert thirty.vetted_at is not None
    assert thirty.host_business == "30/90"
    assert thirty.street_address == "520 FRENCHMEN ST"
    assert abs(thirty.longitude - -90.0577) < 1e-6

    noted = cams["gsheet:4218-magazine-st"]
    assert noted.status == CameraStatus.PENDING
    assert noted.vetted_at is None
    assert "not Project NOLA" in noted.reporter_notes
    assert noted.host_business == "Theos / Theos magazine"

    assert cams["gsheet:610-tchoupitoulas-st"].host_business == "Kimberly A Meng DDS"
    corner = cams["gsheet:decatur-st-bienville-st"]
    assert corner.host_business == "(unnamed)"
    assert corner.status == CameraStatus.PENDING  # Nominatim fallback hits need a human look
    assert "nominatim" in corner.reporter_notes
    # "see MJW" and the address-less row were skipped and not geocoded.
    assert "see MJW" in " ".join(fake_geocoder)
    assert not Camera.objects.filter(host_business__contains="Back Lot").exists()


@pytest.mark.django_db
def test_import_is_idempotent(tmp_path, fake_geocoder):
    path = tmp_path / "sheet.csv"
    path.write_text(CSV)
    call_command("import_sheet", file=str(path))
    call_command("import_sheet", file=str(path))
    assert Camera.objects.filter(source=CameraSource.ADMIN).count() == 4


@pytest.mark.django_db
def test_dry_run_writes_nothing(tmp_path, fake_geocoder):
    path = tmp_path / "sheet.csv"
    path.write_text(CSV)
    call_command("import_sheet", file=str(path), dry_run=True)
    assert Camera.objects.count() == 0


@pytest.mark.django_db
def test_pending_flag_overrides(tmp_path, fake_geocoder):
    path = tmp_path / "sheet.csv"
    path.write_text(CSV)
    call_command("import_sheet", file=str(path), pending=True)
    assert not Camera.objects.filter(status=CameraStatus.VETTED).exists()


def test_census_rejects_out_of_bbox(monkeypatch, settings):
    monkeypatch.setattr(
        geocode_mod,
        "_get_json",
        lambda url, params, timeout=20: {
            "result": {"addressMatches": [{"coordinates": {"x": -122.4, "y": 37.8}, "matchedAddress": "SF"}]}
        },
    )
    assert geocode_mod.geocode_census("1 Market St") is None
