"""Write-ahead audit.

The rule this file exists to enforce: the log never claims less than what
happened. Intent is written *before* the work starts; the outcome is a
second row that points back at the intent row. Nothing is ever edited or
deleted — the application role is not granted UPDATE or DELETE on this
table, so that is enforced by Postgres and not by good intentions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import psycopg


@dataclass(frozen=True)
class AuditEntry:
    id: int
    event: str
    phase: str


@dataclass
class AuditLog:
    conn: psycopg.Connection[tuple[object, ...]]
    entity_id: str
    actor: str
    _pending: list[int] = field(default_factory=list)

    def intent(
        self,
        event: str,
        detail: dict[str, Any] | None = None,
        task_id: str | None = None,
        proposal_id: str | None = None,
    ) -> AuditEntry:
        """Record what is about to be attempted. Call before doing it."""
        return self._write("intent", event, detail or {}, task_id, proposal_id)

    def outcome(
        self,
        intent: AuditEntry,
        detail: dict[str, Any] | None = None,
        task_id: str | None = None,
        proposal_id: str | None = None,
    ) -> AuditEntry:
        """Record what actually happened, pointing back at the intent row."""
        payload = dict(detail or {})
        payload["intent_id"] = intent.id
        return self._write("outcome", intent.event, payload, task_id, proposal_id)

    def _write(
        self,
        phase: str,
        event: str,
        detail: dict[str, Any],
        task_id: str | None,
        proposal_id: str | None,
    ) -> AuditEntry:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (entity_id, actor, task_id, proposal_id, event,
                                       phase, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.entity_id,
                    self.actor,
                    task_id,
                    proposal_id,
                    event,
                    phase,
                    json.dumps(detail, default=str),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return AuditEntry(id=int(str(row[0])), event=event, phase=phase)


__all__ = ["AuditEntry", "AuditLog"]
