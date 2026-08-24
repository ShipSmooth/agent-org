"""Append-only audit log with write-ahead discipline.

An ``intent`` row is written BEFORE the thing happens, an ``outcome`` row
after — a second INSERT referencing the intent row id, never an UPDATE —
so the log never claims less than what actually happened.
"""

from __future__ import annotations

import json

import psycopg


def audit(
    conn: psycopg.Connection,
    entity_id: str,
    *,
    actor: str,
    event: str,
    phase: str,  # 'intent' | 'outcome'
    task_id: str | None = None,
    proposal_id: str | None = None,
    detail: dict[str, object] | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO audit_log (entity_id, actor, task_id, proposal_id, event, phase, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            entity_id,
            actor,
            task_id,
            proposal_id,
            event,
            phase,
            json.dumps(detail or {}),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])
