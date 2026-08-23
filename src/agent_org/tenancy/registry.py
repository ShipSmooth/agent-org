"""The entity registry — the one table that is not entity-scoped.

Registering a business is an administrative act performed with the
migrator role, not something an agent can do mid-run.
"""

from __future__ import annotations

import json

import psycopg

from agent_org.config.models import EntityConfig


def register_entity(conn: psycopg.Connection[tuple[object, ...]], entity: EntityConfig) -> None:
    """Insert or refresh one entity row. Idempotent."""
    config = {
        "credentials_prefix": entity.credentials_prefix,
        "channels": [
            {
                "name": channel.name,
                "key": channel.key,
                "fulfillment": channel.fulfillment,
                "has_history": channel.has_history,
            }
            for channel in entity.channels
        ],
        "agents": [
            {"name": agent.name, "kind": agent.kind, "schedule": agent.schedule}
            for agent in entity.agents
        ],
    }
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO entities (id, legal_name, status, timezone, config)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
               SET legal_name = EXCLUDED.legal_name,
                   status     = EXCLUDED.status,
                   timezone   = EXCLUDED.timezone,
                   config     = EXCLUDED.config,
                   updated_at = now()
            """,
            (
                entity.entity_id,
                entity.legal_name,
                entity.status,
                entity.timezone,
                json.dumps(config),
            ),
        )


def entity_exists(conn: psycopg.Connection[tuple[object, ...]], entity_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM entities WHERE id = %s", (entity_id,))
        return cur.fetchone() is not None


__all__ = ["entity_exists", "register_entity"]
