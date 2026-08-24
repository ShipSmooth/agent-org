"""Postgres connections.

Two roles exist (docs/data-model.md): ``agent_org_migrator`` owns the
tables and runs migrations; ``agent_org_app`` is the application role —
no BYPASSRLS, not the owner. Connection strings come from environment
variables only.
"""

from __future__ import annotations

import os

import psycopg


class MissingDatabaseUrlError(RuntimeError):
    """Raised when no database connection string is configured."""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise MissingDatabaseUrlError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    return url


def migrator_url() -> str:
    url = os.environ.get("MIGRATOR_DATABASE_URL", "")
    if url:
        return url
    return database_url()


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())
