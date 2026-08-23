"""Migrations: numbered SQL files, applied once, in order.

No migration framework. The files are plain SQL so that what runs against
the database is exactly what is reviewable in the diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from agent_org.db.connection import APP_ROLE

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class MigrationResult:
    applied: tuple[str, ...]
    already_applied: tuple[str, ...]


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(directory.glob("*.sql"))


def ensure_app_role(
    conn: psycopg.Connection[tuple[object, ...]], password: str, role: str = APP_ROLE
) -> None:
    """Create the unprivileged application role if it is not there yet.

    The password comes from the environment. A role with no password is
    created only when none is supplied, which is the case in tests where
    the database is reachable on a local socket.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        exists = cur.fetchone() is not None
        if not exists:
            statement = sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
            if password:
                statement = sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            cur.execute(statement)
        elif password:
            cur.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        cur.execute(
            sql.SQL("ALTER ROLE {} NOBYPASSRLS NOSUPERUSER NOCREATEDB").format(sql.Identifier(role))
        )


def run_migrations(
    conn: psycopg.Connection[tuple[object, ...]], directory: Path = MIGRATIONS_DIR
) -> MigrationResult:
    applied: list[str] = []
    skipped: list[str] = []
    with conn.transaction():
        conn.execute(SCHEMA_MIGRATIONS_DDL)
    for path in migration_files(directory):
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename = %s", (path.name,))
            if cur.fetchone() is not None:
                skipped.append(path.name)
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return MigrationResult(applied=tuple(applied), already_applied=tuple(skipped))


__all__ = ["MIGRATIONS_DIR", "MigrationResult", "ensure_app_role", "run_migrations"]
