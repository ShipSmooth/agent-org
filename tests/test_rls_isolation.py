"""Entity isolation is enforced by Postgres, not by application code.

These tests run as the application role (not the table owner, no
BYPASSRLS), which is how the worker connects in production.
"""

from __future__ import annotations

import psycopg
import pytest

from agent_org.db.migrate import APP_ROLE
from agent_org.tenancy.session import entity_session


def _seed(conn: psycopg.Connection, entity_id: str, part: str) -> None:
    with entity_session(conn, entity_id):
        row = conn.execute(
            "INSERT INTO suppliers (entity_id, name) VALUES (%s, 'NAR') "
            "ON CONFLICT (entity_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (entity_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO components (entity_id, supplier_id, supplier_part_no, name, class) "
            "VALUES (%s, %s, %s, %s, 'forecast') ON CONFLICT DO NOTHING",
            (entity_id, row[0], part, f"component {part}"),
        )
    conn.commit()


def test_application_role_is_not_the_table_owner_and_cannot_bypass_rls(
    db: psycopg.Connection,
) -> None:
    row = db.execute(
        "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = %s", (APP_ROLE,)
    ).fetchone()
    assert row == (False, False)
    owners = db.execute(
        "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    assert APP_ROLE not in {o[0] for o in owners}


def test_entity_a_cannot_read_entity_b(
    clean_db: psycopg.Connection, app_db: psycopg.Connection
) -> None:
    _seed(app_db, "ithrive", "30-0001")
    _seed(app_db, "lima_zulu", "LZ-0001")

    with entity_session(app_db, "ithrive"):
        parts = [r[0] for r in app_db.execute("SELECT supplier_part_no FROM components").fetchall()]
    assert parts == ["30-0001"]


def test_a_deliberately_wrong_query_still_cannot_reach_the_other_entity(
    clean_db: psycopg.Connection, app_db: psycopg.Connection
) -> None:
    """Application code asking for the wrong entity_id gets nothing, not data."""
    _seed(app_db, "ithrive", "30-0001")
    _seed(app_db, "lima_zulu", "LZ-0001")

    with entity_session(app_db, "ithrive"):
        rows = app_db.execute(
            "SELECT supplier_part_no FROM components WHERE entity_id = 'lima_zulu'"
        ).fetchall()
        assert rows == []
        rows = app_db.execute(
            "SELECT supplier_part_no FROM components WHERE true OR 1=1"
        ).fetchall()
        assert [r[0] for r in rows] == ["30-0001"]


def test_writing_another_entitys_row_is_refused(
    clean_db: psycopg.Connection, app_db: psycopg.Connection
) -> None:
    with pytest.raises(psycopg.errors.Error), entity_session(app_db, "ithrive"):
        app_db.execute("INSERT INTO suppliers (entity_id, name) VALUES ('lima_zulu', 'NAR')")
    app_db.rollback()
    with entity_session(app_db, "lima_zulu"):
        assert app_db.execute("SELECT count(*) FROM suppliers").fetchone() == (0,)


def test_an_unset_entity_scope_fails_loudly_rather_than_returning_nothing(
    clean_db: psycopg.Connection, app_db: psycopg.Connection
) -> None:
    """Silent zero rows would look like 'nothing to order'. It must raise."""
    with pytest.raises(psycopg.errors.Error):
        app_db.execute("SELECT * FROM components").fetchall()
    app_db.rollback()


def test_the_scope_does_not_leak_past_the_transaction(
    clean_db: psycopg.Connection, app_db: psycopg.Connection
) -> None:
    _seed(app_db, "ithrive", "30-0001")
    with entity_session(app_db, "ithrive"):
        pass
    with pytest.raises(psycopg.errors.Error):
        app_db.execute("SELECT * FROM components").fetchall()
    app_db.rollback()


def test_every_business_table_has_forced_row_level_security(db: psycopg.Connection) -> None:
    """'entities' is the registry the scoping keys into, so it is exempt by design;
    'schema_migrations' holds no business data."""
    rows = db.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
        "AND c.relname NOT IN ('schema_migrations', 'entities')"
    ).fetchall()
    assert rows
    unprotected = [name for name, enabled, forced in rows if not (enabled and forced)]
    assert unprotected == []
