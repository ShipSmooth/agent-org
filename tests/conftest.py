"""Shared fixtures. Database tests skip cleanly when no Postgres is configured."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from agent_org.db.migrate import migrate
from agent_org.integrations.gmail import GmailReadClient, OnOrderResult
from agent_org.integrations.veeqo import VeeqoReadClient, VeeqoSnapshot
from agent_org.shannon.config_model import EntityConfig, load_entity_config

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


@pytest.fixture(scope="session")
def golden_config_dir() -> Path:
    return GOLDEN / "config"


@pytest.fixture(scope="session")
def golden_data_dir() -> Path:
    return GOLDEN / "data"


@pytest.fixture
def golden_cfg(golden_config_dir: Path) -> EntityConfig:
    return load_entity_config(golden_config_dir, "ithrive")


@pytest.fixture
def golden_snapshot(golden_data_dir: Path) -> VeeqoSnapshot:
    return VeeqoReadClient(golden_data_dir).snapshot()


@pytest.fixture
def golden_on_order(golden_data_dir: Path) -> OnOrderResult:
    return GmailReadClient(golden_data_dir).on_order()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("MIGRATOR_DATABASE_URL")
    if not url:
        pytest.skip("No TEST_DATABASE_URL / MIGRATOR_DATABASE_URL — skipping database tests.")
    migrate(url)
    return url


@pytest.fixture
def db(migrated_database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(migrated_database) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def clean_db(db: psycopg.Connection) -> psycopg.Connection:
    for table in (
        "audit_log",
        "agent_runs",
        "approvals",
        "action_proposals",
        "reports",
        "tasks",
        "order_history",
        "boms",
        "products",
        "components",
        "suppliers",
        "channels",
        "entities",
    ):
        db.execute(f"DELETE FROM {table}")
    db.execute(
        "INSERT INTO entities (id, legal_name, status, timezone) "
        "VALUES ('ithrive', 'iThrive Medical LLC', 'active', 'America/New_York'), "
        "('lima_zulu', 'Lima Zulu Group LLC', 'active', 'America/New_York') "
        "ON CONFLICT (id) DO NOTHING"
    )
    db.commit()
    return db


@pytest.fixture
def app_db(migrated_database: str) -> Iterator[psycopg.Connection]:
    """A connection as the application role: not the owner, no BYPASSRLS."""
    url = os.environ.get("TEST_APP_DATABASE_URL")
    if not url:
        pytest.skip("No TEST_APP_DATABASE_URL — skipping row-level-security tests.")
    with psycopg.connect(url) as conn:
        yield conn
        conn.rollback()
