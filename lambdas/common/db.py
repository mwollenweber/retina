"""
Postgres connection helper for the Lambdas.

* If ``DATABASE_URL`` is set, it is used directly (``postgres://`` or
  ``postgresql://`` URI; psycopg understands both).
* Otherwise ``DB_SECRET_ARN`` names a Secrets Manager secret in the standard
  RDS JSON shape (``username``, ``password``, ``host``, ``port``, ``dbname``).
  The secret is fetched once and cached in the module for the lifetime of the
  execution environment, so warm invocations do not call Secrets Manager.

psycopg is imported lazily inside :func:`get_conn` so that the pure
transformation code in each handler can be imported (and unit tested) without
a psycopg wheel being available for the local interpreter.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)

_SECRET_CACHE: dict[str, dict] = {}


def _load_secret(secret_arn: str, client=None) -> dict:
    """Fetch and cache an RDS-style secret. ``client`` is injectable for tests."""
    cached = _SECRET_CACHE.get(secret_arn)
    if cached is not None:
        return cached
    if client is None:
        import boto3  # provided by the Lambda runtime

        client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(resp["SecretString"])
    _SECRET_CACHE[secret_arn] = secret
    return secret


def clear_secret_cache() -> None:
    _SECRET_CACHE.clear()


def dsn_from_secret(secret: dict, sslmode: str = "require") -> str:
    """Build a libpq URI from the standard RDS secret JSON."""
    user = quote(str(secret["username"]), safe="")
    password = quote(str(secret["password"]), safe="")
    host = secret["host"]
    port = secret.get("port", 5432)
    dbname = secret.get("dbname") or secret.get("database") or "postgres"
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}?sslmode={sslmode}"


def resolve_dsn(env: dict | None = None, secret_client=None) -> str:
    """Decide the DSN from the environment without opening a connection."""
    env = os.environ if env is None else env
    url = env.get("DATABASE_URL")
    if url:
        return url
    secret_arn = env.get("DB_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("Set DATABASE_URL or DB_SECRET_ARN")
    secret = _load_secret(secret_arn, client=secret_client)
    return dsn_from_secret(secret, sslmode=env.get("DB_SSLMODE", "require"))


def get_conn():
    """
    Open a psycopg 3 connection with dict rows.

    Returns a connection usable as a context manager (``with get_conn() as conn:``);
    psycopg commits on clean exit and rolls back on exception, then closes.
    The sync_log context manager manages commits explicitly within that.
    """
    import psycopg
    from psycopg.rows import dict_row

    dsn = resolve_dsn()
    timeout = int(os.environ.get("DB_CONNECT_TIMEOUT", "10"))
    return psycopg.connect(
        dsn,
        row_factory=dict_row,
        connect_timeout=timeout,
        application_name=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "retina-lambda"),
    )
