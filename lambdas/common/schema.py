"""
Table names and choice values used by the Lambdas.

THIS FILE MUST MIRROR ``backend/cameras/models.py``. The Django app owns the
schema; the Lambdas write to the pinned tables directly with psycopg. If a
column, table name, or choice value changes in models.py, change it here too.

Column naming: columns equal Django field names, except foreign keys get an
``_id`` suffix (``camera_id``, ``vetted_by_id``, ``reviewed_by_id``).
``camera.location`` is PostGIS ``geography(Point, 4326)``.
"""

# --- Tables (Meta.db_table) ------------------------------------------------
CAMERA_TABLE = "camera"
CAMERA_PHOTO_TABLE = "camera_photo"
SOURCE_SYNC_TABLE = "source_sync"


# --- Camera.camera_type (CameraType) ---------------------------------------
class CameraType:
    PROJECT_NOLA = "project_nola"
    NOPD = "nopd"
    TRAFFIC = "traffic"
    ALPR = "alpr"
    PRIVATE = "private"
    UNKNOWN = "unknown"

    ALL = (PROJECT_NOLA, NOPD, TRAFFIC, ALPR, PRIVATE, UNKNOWN)


# --- Camera.status (CameraStatus) ------------------------------------------
class CameraStatus:
    PENDING = "pending"
    VETTED = "vetted"
    REJECTED = "rejected"

    ALL = (PENDING, VETTED, REJECTED)


# --- Camera.source (CameraSource) ------------------------------------------
class CameraSource:
    FIELD_REPORT = "field_report"
    ADMIN = "admin"
    CITY_TRAFFIC = "city_traffic"
    OSM = "osm"
    RECORDS_REQUEST = "records_request"

    ALL = (FIELD_REPORT, ADMIN, CITY_TRAFFIC, OSM, RECORDS_REQUEST)


# --- Camera.mount (Mount) ---------------------------------------------------
class Mount:
    POLE = "pole"
    BUILDING = "building"
    TRAFFIC_SIGNAL = "traffic_signal"
    OTHER = "other"
    UNKNOWN = ""

    ALL = (POLE, BUILDING, TRAFFIC_SIGNAL, OTHER, UNKNOWN)


# --- CameraPhoto.photo_type (PhotoType) ------------------------------------
class PhotoType:
    CLOSE_UP = "close_up"
    SURROUNDING = "surrounding"
    SIGNAGE = "signage"

    ALL = (CLOSE_UP, SURROUNDING, SIGNAGE)


# --- CameraPhoto.status (PhotoStatus) --------------------------------------
class PhotoStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL = (PENDING, APPROVED, REJECTED)


# --- SourceSync.status (SourceSync.RunStatus) ------------------------------
class RunStatus:
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    ALL = (RUNNING, SUCCEEDED, FAILED)


# --- Camera columns the Lambdas are allowed to write -----------------------
# Columns that must NEVER be published or touched by sync jobs are listed so
# publish_geojson can assert it is not leaking them.
CAMERA_PRIVATE_COLUMNS = ("reporter_contact", "reporter_notes", "admin_notes")
PHOTO_PRIVATE_COLUMNS = ("submitted_by",)

# Max length of source_sync.source (CharField(max_length=40)).
SOURCE_SYNC_SOURCE_MAX_LEN = 40
