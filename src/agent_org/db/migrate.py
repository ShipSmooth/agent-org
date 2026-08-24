"""Migration runner.

Applies the SQL files in ``migrations/`` in filename order, exactly once
each, recorded in ``schema_migrations``. Runs as ``agent_org_migrator``
(the table owner). Afterwards it ensures the application role
``agent_org_app`` exists with least privilege: no BYPASSRLS, owns no
tables, cannot UPDATE/DELETE/TRUNCATE the append-only ``audit_log``.
"""

from __future__ import annotations

import os
from importlib import resources

import psycopg
from psycopg import sql

from agent_org.db.connection import connect, migrator_url

APP_ROLE = "agent_org_app"

# Tables the app role may write. audit_log is INSERT/SELECT only.
_RW_TABLES = [
    "suppliers",
    "components",
    "products",
    "boms",
    "channels",
    "tasks",
    "action_proposals",
    "approvals",
    "agent_runs",
    "order_history",
    "reports",
    "entities",
]


def _migration_files() -> list[tuple[str, str]]:
    pkg = resources.files("agent_org.db").joinpath("migrations")
    out: list[tuple[str, str]] = []
    for entry in sorted(pkg.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".sql"):
            out.append((entry.name, entry.read_text(encoding="utf-8")))
    return out


def _ensure_app_role(conn: psycopg.Connection) -> None:
    password = os.environ.get("APP_DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone()
    if row is None:
        if not password:
            raise RuntimeError(
                "POSTGRES_PASSWORD (or APP_DB_PASSWORD) must be set to create the "
                f"application role {APP_ROLE}."
            )
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(APP_ROLE), sql.Literal(password)
            )
        )
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(APP_ROLE)))
    conn.execute(
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in _RW_TABLES),
            sql.Identifier(APP_ROLE),
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT, INSERT ON audit_log TO {}").format(sql.Identifier(APP_ROLE))
    )
    conn.execute(
        sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {}").format(
            sql.Identifier(APP_ROLE)
        )
    )


def migrate(url: str | None = None) -> list[str]:
    """Apply pending migrations; returns the names applied."""
    applied: list[str] = []
    with connect(url or migrator_url()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for name, body in _migration_files():
            if name in done:
                continue
            conn.execute(body)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
        _ensure_app_role(conn)
        conn.commit()
    return applied
