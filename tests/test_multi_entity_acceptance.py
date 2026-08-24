"""Entity isolation, enforced by Postgres.

Two acceptance criteria from docs/multi-entity.md:

* a second business is a new config file and rows in the database, never a
  code change;
* one entity cannot read another's rows even when the query deliberately
  tries to, because the isolation is a database policy rather than a
  `WHERE` clause someone has to remember.
"""

from __future__ import annotations

import psycopg
import pytest

from agent_org.config.models import LoadedConfig
from agent_org.db.connection import APP_ROLE, entity_session
from agent_org.db.sync import sync_config
from agent_org.tenancy.registry import entity_exists, register_entity

pytestmark = pytest.mark.usefixtures("app_dsn")

OTHER = "limazulu"


def _register(conn: psycopg.Connection[tuple[object, ...]], entity_id: str, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO entities (id, legal_name, status, timezone)
            VALUES (%s, %s, 'active', 'America/New_York')
            ON CONFLICT (id) DO NOTHING
            """,
            (entity_id, name),
        )


def test_add_entity_with_zero_code_changes(
    owner_conn: psycopg.Connection[tuple[object, ...]],
    app_conn: psycopg.Connection[tuple[object, ...]],
    golden_config: LoadedConfig,
) -> None:
    """Registering a business and loading its parts list touches no source."""
    register_entity(owner_conn, golden_config.entity)
    owner_conn.commit()
    assert entity_exists(app_conn, golden_config.entity_id)

    with entity_session(app_conn, golden_config.entity_id) as scoped:
        counts = sync_config(scoped, golden_config)
    assert counts["components"] == len(golden_config.boms.components)
    assert counts["kits"] == len(golden_config.boms.kits)


def test_one_entity_cannot_read_anothers_rows(
    owner_conn: psycopg.Connection[tuple[object, ...]],
    app_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _register(owner_conn, "ithrive", "iThrive Medical LLC")
    _register(owner_conn, OTHER, "Lima Zulu Group LLC")
    owner_conn.commit()

    with entity_session(app_conn, OTHER) as scoped, scoped.cursor() as cur:
        cur.execute(
            "INSERT INTO suppliers (entity_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (OTHER, "a-supplier-of-the-other-business"),
        )
    app_conn.commit()

    # The deliberately wrong query: no entity filter at all, and then one
    # naming the other business outright. Both come back empty.
    with entity_session(app_conn, "ithrive") as scoped, scoped.cursor() as cur:
        cur.execute("SELECT entity_id, name FROM suppliers")
        assert all(row[0] == "ithrive" for row in cur.fetchall())

        cur.execute("SELECT count(*) FROM suppliers WHERE entity_id = %s", (OTHER,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_writing_another_entitys_row_is_refused(
    owner_conn: psycopg.Connection[tuple[object, ...]],
    app_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _register(owner_conn, "ithrive", "iThrive Medical LLC")
    _register(owner_conn, OTHER, "Lima Zulu Group LLC")
    owner_conn.commit()

    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        entity_session(app_conn, "ithrive") as scoped,
        scoped.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO suppliers (entity_id, name) VALUES (%s, %s)",
            (OTHER, "smuggled-in"),
        )
    app_conn.rollback()


def test_a_query_with_no_entity_scope_is_an_error(
    app_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """The one-argument current_setting raises rather than returning nothing."""
    with pytest.raises(psycopg.errors.UndefinedObject), app_conn.cursor() as cur:
        cur.execute("SELECT * FROM suppliers")
    app_conn.rollback()


def test_the_scope_does_not_outlive_its_transaction(
    owner_conn: psycopg.Connection[tuple[object, ...]],
    app_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """A scope left set after a run would silently attribute the next
    business's work to the last one."""
    _register(owner_conn, "ithrive", "iThrive Medical LLC")
    owner_conn.commit()
    with entity_session(app_conn, "ithrive") as scoped, scoped.cursor() as cur:
        cur.execute("SELECT count(*) FROM suppliers")
    app_conn.commit()

    with pytest.raises(psycopg.errors.UndefinedObject), app_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM suppliers")
    app_conn.rollback()


def test_the_application_role_cannot_own_or_bypass_the_policies(
    owner_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """Isolation only holds if the worker's own role cannot step around it."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT rolbypassrls, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = %s",
            (APP_ROLE,),
        )
        assert cur.fetchone() == (False, False, False, False)

        cur.execute("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
        assert APP_ROLE not in {row[0] for row in cur.fetchall()}


def test_every_business_table_forces_row_level_security(
    owner_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """FORCE matters as much as ENABLE: without it the owner — and any
    future migration run as the owner — quietly sees everything.

    `entities` is the registry the scoping keys into and `schema_migrations`
    holds no business data, so both are exempt by design.
    """
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND c.relname NOT IN ('schema_migrations', 'entities')"
        )
        rows = cur.fetchall()
    assert rows
    assert [name for name, enabled, forced in rows if not (enabled and forced)] == []
