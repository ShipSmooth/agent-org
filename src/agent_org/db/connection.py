"""Database connections, and the one place entity scope is set.

Every application query runs inside `entity_session`, which opens a
transaction and sets `app.entity_id` for its duration. The row-level
security policies read that setting; a connection that skipped it gets an
error from Postgres rather than another business's rows.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql

APP_ROLE = "agent_org_app"


class DatabaseNotConfigured(RuntimeError):
    """Raised when the database connection details are missing from the environment."""


@dataclass(frozen=True)
class DatabaseSettings:
    """Connection settings. Secrets come from the environment, never source."""

    app_dsn: str
    migrator_dsn: str
    app_password: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DatabaseSettings:
        environ = dict(os.environ if env is None else env)
        app_dsn = environ.get("DATABASE_URL", "")
        migrator_dsn = environ.get("DATABASE_MIGRATOR_URL", "")
        if not app_dsn:
            raise DatabaseNotConfigured(
                "DATABASE_URL is not set. Copy .env.example to .env and fill it in, "
                "then start the database with `docker compose up -d`."
            )
        return cls(
            app_dsn=app_dsn,
            migrator_dsn=migrator_dsn or app_dsn,
            app_password=environ.get("POSTGRES_APP_PASSWORD", ""),
        )


def connect(dsn: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(dsn, autocommit=False)


@contextmanager
def entity_session(
    conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """Run a transaction scoped to one entity.

    `SET LOCAL` lasts exactly as long as the transaction, so scope cannot
    leak into the next piece of work on the same connection.
    """
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL app.entity_id = {}").format(sql.Literal(entity_id)))
        yield conn


__all__ = [
    "APP_ROLE",
    "DatabaseNotConfigured",
    "DatabaseSettings",
    "connect",
    "entity_session",
]
