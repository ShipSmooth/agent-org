"""Test fixtures, including a real Postgres.

The database tests run against a real server because the thing they are
testing — row-level security — is a Postgres feature. A fake would prove
nothing. Point AGENT_ORG_TEST_MIGRATOR_URL at a throwaway database, or let
it default to the one in docker-compose.yml; the tests skip if neither is
reachable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from agent_org.config.loader import load_config
from agent_org.config.models import LoadedConfig
from agent_org.db.connection import APP_ROLE, set_app_password
from agent_org.db.migrate import run_migrations

DEFAULT_MIGRATOR_URL = "postgresql://agent_org_migrator:devpassword@127.0.0.1:5433/agent_org"
# Local test role only; never a real credential.
APP_TEST_PASSWORD = "test-only-not-a-secret"
GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def migrator_url() -> str:
    return os.environ.get("AGENT_ORG_TEST_MIGRATOR_URL", DEFAULT_MIGRATOR_URL)


@pytest.fixture(scope="session")
def migrator_dsn() -> str:
    dsn = migrator_url()
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            pass
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"No Postgres to test against ({exc.__class__.__name__}). Run docker compose.")
    return dsn


@pytest.fixture(scope="session")
def app_dsn(migrator_dsn: str) -> str:
    """Migrate the schema and hand back a DSN for the *application* role.

    Everything the application does runs as that role, which cannot bypass
    row-level security. Testing as the owner would prove nothing.
    """
    with psycopg.connect(migrator_dsn) as conn:
        run_migrations(conn)
        set_app_password(conn, APP_TEST_PASSWORD)
        conn.commit()
    info = {
        key: str(value)
        for key, value in psycopg.conninfo.conninfo_to_dict(migrator_dsn).items()
        if value is not None
    }
    info["user"] = APP_ROLE
    info["password"] = APP_TEST_PASSWORD
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture
def app_conn(app_dsn: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(app_dsn) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def owner_conn(migrator_dsn: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(migrator_dsn) as conn:
        yield conn
        conn.commit()


@pytest.fixture
def entity_id(owner_conn: psycopg.Connection[tuple[object, ...]], app_dsn: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO entities (id, legal_name, status, timezone)
            VALUES ('ithrive', 'iThrive Medical LLC', 'active', 'America/New_York')
            ON CONFLICT (id) DO NOTHING
            """
        )
        # Each test starts from an empty ledger; rows left by the last run
        # would otherwise be claimed by this one.
        cur.execute("DELETE FROM audit_log WHERE entity_id = 'ithrive'")
        cur.execute("DELETE FROM action_proposals WHERE entity_id = 'ithrive'")
        # Both of these point at reports, and reports are never deleted in
        # earnest — only here, to give each test an empty ledger.
        cur.execute("DELETE FROM report_emails WHERE entity_id = 'ithrive'")
        cur.execute("DELETE FROM manual_stock_proposals WHERE entity_id = 'ithrive'")
        cur.execute("DELETE FROM cart_stagings WHERE entity_id = 'ithrive'")
        cur.execute("DELETE FROM reports WHERE entity_id = 'ithrive'")
        cur.execute("DELETE FROM tasks WHERE entity_id = 'ithrive'")
    owner_conn.commit()
    return "ithrive"


@pytest.fixture(scope="session")
def golden_config() -> LoadedConfig:
    return load_config(GOLDEN / "config", "ithrive")[0]
