"""
Context manager that records one ``source_sync`` row per job run.

    with sync_run(conn, "city_traffic") as stats:
        ...
        stats.seen += 1
        stats.created += 1

On entry it inserts a ``running`` row and commits immediately, so the row is
visible in the admin even if the job later dies. On clean exit it marks the
row ``succeeded`` with the counts and commits the job's work in the same
transaction. On exception it rolls back the job's work, marks the row
``failed`` with the error text, commits that, and re-raises.
"""

from __future__ import annotations

import logging
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common.schema import RunStatus, SOURCE_SYNC_SOURCE_MAX_LEN, SOURCE_SYNC_TABLE

logger = logging.getLogger(__name__)

MAX_ERROR_LEN = 8000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SyncStats:
    source: str
    id: int | None = None
    seen: int = 0
    created: int = 0
    updated: int = 0
    started_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict:
        return {
            "sync_id": self.id,
            "source": self.source,
            "seen": self.seen,
            "created": self.created,
            "updated": self.updated,
        }


INSERT_SQL = f"""
INSERT INTO {SOURCE_SYNC_TABLE}
    (source, status, started_at, finished_at, records_seen, records_created, records_updated, error)
VALUES
    (%(source)s, %(status)s, %(started_at)s, NULL, 0, 0, 0, '')
RETURNING id
"""

FINISH_SQL = f"""
UPDATE {SOURCE_SYNC_TABLE}
SET status = %(status)s,
    finished_at = %(finished_at)s,
    records_seen = %(seen)s,
    records_created = %(created)s,
    records_updated = %(updated)s,
    error = %(error)s
WHERE id = %(id)s
"""


def _finish(conn, stats: SyncStats, status: str, error: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute(
            FINISH_SQL,
            {
                "status": status,
                "finished_at": utcnow(),
                "seen": stats.seen,
                "created": stats.created,
                "updated": stats.updated,
                "error": error[:MAX_ERROR_LEN],
                "id": stats.id,
            },
        )


@contextmanager
def sync_run(conn, source: str):
    if len(source) > SOURCE_SYNC_SOURCE_MAX_LEN:
        raise ValueError(f"source_sync.source longer than {SOURCE_SYNC_SOURCE_MAX_LEN}: {source!r}")
    stats = SyncStats(source=source)
    with conn.cursor() as cur:
        cur.execute(
            INSERT_SQL,
            {"source": source, "status": RunStatus.RUNNING, "started_at": stats.started_at},
        )
        row = cur.fetchone()
        stats.id = row["id"] if isinstance(row, dict) else row[0]
    conn.commit()
    logger.info("source_sync %s started (id=%s)", source, stats.id)

    try:
        yield stats
        _finish(conn, stats, RunStatus.SUCCEEDED)
        conn.commit()
        logger.info("source_sync %s succeeded: %s", source, stats.as_dict())
    except BaseException as exc:  # noqa: BLE001 - we re-raise after logging
        conn.rollback()
        err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        try:
            _finish(conn, stats, RunStatus.FAILED, error=err)
            conn.commit()
        except Exception:  # pragma: no cover - best effort
            logger.exception("could not record failure for source_sync id=%s", stats.id)
        logger.error("source_sync %s failed: %s", source, err)
        raise
