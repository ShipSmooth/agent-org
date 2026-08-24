"""Task queue machinery — write-ahead state transitions.

Tasks are rows; the state transition is written BEFORE work begins and
updated after, each transition mirrored to the audit log, so a crash
leaves an honest record. Claiming uses FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import psycopg

from agent_org.runtime.audit import audit
from agent_org.tenancy.session import entity_session


@dataclass(frozen=True)
class Task:
    id: str
    entity_id: str
    kind: str
    schedule_slot: str
    state: str
    attempts: int
    payload: dict[str, object]


def enqueue(
    conn: psycopg.Connection,
    entity_id: str,
    kind: str,
    schedule_slot: str,
    payload: dict[str, object] | None = None,
) -> str | None:
    """Insert a QUEUED task; one per (entity, kind, slot). Returns id or None if it exists."""
    with entity_session(conn, entity_id):
        row = conn.execute(
            """
            INSERT INTO tasks (entity_id, kind, schedule_slot, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_id, kind, schedule_slot) DO NOTHING
            RETURNING id
            """,
            (entity_id, kind, schedule_slot, json.dumps(payload or {})),
        ).fetchone()
        if row is None:
            return None
        task_id = str(row[0])
        audit(
            conn,
            entity_id,
            actor="scheduler",
            event="task.state",
            phase="intent",
            task_id=task_id,
            detail={"state": "QUEUED", "kind": kind, "schedule_slot": schedule_slot},
        )
        return task_id


def claim(
    conn: psycopg.Connection, entity_id: str, kind: str, schedule_slot: str | None = None
) -> Task | None:
    """Claim a QUEUED task: mark RUNNING (write-ahead) and return it.

    Naming a slot claims that slot and nothing else, so a manual run for one
    week never picks up a different week's queued task.
    """
    with entity_session(conn, entity_id):
        row = conn.execute(
            """
            SELECT id, kind, schedule_slot, attempts, payload FROM tasks
            WHERE entity_id = %s AND kind = %s AND state = 'QUEUED'
              AND (%s::text IS NULL OR schedule_slot = %s)
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (entity_id, kind, schedule_slot, schedule_slot),
        ).fetchone()
        if row is None:
            return None
        task_id = str(row[0])
        conn.execute(
            """
            UPDATE tasks
            SET state = 'RUNNING', attempts = attempts + 1,
                heartbeat_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (task_id,),
        )
        audit(
            conn,
            entity_id,
            actor="worker",
            event="task.state",
            phase="intent",
            task_id=task_id,
            detail={"state": "RUNNING", "attempt": int(row[3]) + 1},
        )
        payload = row[4] if isinstance(row[4], dict) else {}
        return Task(
            id=task_id,
            entity_id=entity_id,
            kind=str(row[1]),
            schedule_slot=str(row[2]),
            state="RUNNING",
            attempts=int(row[3]) + 1,
            payload=payload,
        )


def finish(
    conn: psycopg.Connection,
    task: Task,
    *,
    state: str,  # 'SUCCEEDED' | 'FAILED'
    error: str | None = None,
) -> None:
    with entity_session(conn, task.entity_id):
        conn.execute(
            "UPDATE tasks SET state = %s, error = %s, updated_at = now() WHERE id = %s",
            (state, error, task.id),
        )
        audit(
            conn,
            task.entity_id,
            actor="worker",
            event="task.state",
            phase="outcome",
            task_id=task.id,
            detail={"state": state, "error": error},
        )


def record_agent_run(
    conn: psycopg.Connection,
    task: Task,
    *,
    agent_kind: str,
    step_count: int,
    wall_ms: int,
    transcript: list[str],
) -> None:
    with entity_session(conn, task.entity_id):
        conn.execute(
            """
            INSERT INTO agent_runs (entity_id, task_id, agent_kind, step_count, wall_ms, transcript)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (task.entity_id, task.id, agent_kind, step_count, wall_ms, json.dumps(transcript)),
        )
