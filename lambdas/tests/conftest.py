"""
Shared fixtures: a tiny in-memory fake psycopg connection.

``FakeConn`` records every ``(sql, params)`` executed and answers queries with
canned rows via a ``responder(sql, params) -> list[dict]`` callable. It is also
a context manager so handlers' ``with get_conn() as conn:`` works unchanged.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

import pytest


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


class FakeCursor:
    def __init__(self, conn: "FakeConn"):
        self.conn = conn
        self._rows: list[dict] = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self._rows = list(self.conn.respond(sql, params) or [])
        self.rowcount = len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, responder: Callable | None = None):
        self.executed: list[tuple[str, dict | None]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._responders: list[tuple[str, Callable]] = []
        self._next_sync_id = 100
        if responder:
            self._responders.append(("", responder))

    # -- configuration -------------------------------------------------
    def on(self, sql_substring: str, responder):
        """Register canned rows (list or callable(sql, params)) for SQL containing a substring."""
        if not callable(responder):
            rows = responder
            responder = lambda sql, params, rows=rows: rows  # noqa: E731
        self._responders.append((_norm(sql_substring), responder))
        return self

    def respond(self, sql, params):
        n = _norm(sql)
        # Default: the sync_log INSERT returns an id.
        if n.startswith("insert into source_sync"):
            self._next_sync_id += 1
            return [{"id": self._next_sync_id}]
        for needle, responder in self._responders:
            if needle in n:
                return responder(sql, params)
        return []

    # -- psycopg surface -----------------------------------------------
    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False

    # -- assertions ----------------------------------------------------
    def statements(self, prefix: str) -> list[tuple[str, dict | None]]:
        p = _norm(prefix)
        return [(s, params) for s, params in self.executed if _norm(s).startswith(p)]

    def sync_rows(self) -> list[dict]:
        """The source_sync finish-updates, in order."""
        return [params for s, params in self.executed if _norm(s).startswith("update source_sync")]


@pytest.fixture
def fake_conn():
    return FakeConn()


@pytest.fixture(autouse=True)
def _no_aws_creds(monkeypatch):
    """Keep boto3 from ever reaching real AWS during tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for var in ("DATABASE_URL", "DB_SECRET_ARN", "OSM_PUSH_ENABLED", "OSM_PUSH_TYPES", "LOCAL_OUTPUT_PATH", "CLOUDFRONT_DISTRIBUTION_ID"):
        monkeypatch.delenv(var, raising=False)
