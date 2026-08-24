"""The task queue — a Postgres table, claimed with FOR UPDATE SKIP LOCKED.

There is no message broker. Volume is a handful of tasks a week, and a
table can be read, audited and corrected by a human with a SQL client,
which a broker cannot.

Every state change is written before the work of the new state begins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import psycopg

from agent_org.audit.log import AuditLog


class TaskState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.REJECTED, TaskState.EXPIRED})


@dataclass(frozen=True)
class Task:
    id: str
    entity_id: str
    kind: str
    state: TaskState
    schedule_slot: str
    attempts: int
    max_attempts: int
    payload: dict[str, Any]
    error: str | None


def _row_to_task(row: tuple[Any, ...]) -> Task:
    return Task(
        id=str(row[0]),
        entity_id=str(row[1]),
        kind=str(row[2]),
        state=TaskState(row[3]),
        schedule_slot=str(row[4]),
        attempts=int(row[5]),
        max_attempts=int(row[6]),
        payload=dict(row[7] or {}),
        error=row[8],
    )


COLUMNS = "id, entity_id, kind, state, schedule_slot, attempts, max_attempts, payload, error"


class TaskQueue:
    """Enqueue, claim and finish tasks for one entity."""

    def __init__(
        self,
        conn: psycopg.Connection[tuple[object, ...]],
        entity_id: str,
        audit: AuditLog,
    ) -> None:
        self.conn = conn
        self.entity_id = entity_id
        self.audit = audit

    def enqueue(
        self,
        kind: str,
        schedule_slot: str,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> Task:
        """Queue one business occurrence. Enqueuing the same slot twice returns
        the existing task rather than running the week twice."""
        intent = self.audit.intent("task.enqueue", {"kind": kind, "schedule_slot": schedule_slot})
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO tasks (entity_id, kind, schedule_slot, payload, max_attempts)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, kind, schedule_slot) DO UPDATE
                    SET updated_at = now()
                RETURNING {COLUMNS}
                """,
                (
                    self.entity_id,
                    kind,
                    schedule_slot,
                    json.dumps(payload or {}),
                    max_attempts,
                ),
            )
            row = cur.fetchone()
            assert row is not None
        task = _row_to_task(row)
        self.audit.outcome(intent, {"task_state": task.state.value}, task_id=task.id)
        return task

    def claim(
        self,
        kinds: tuple[str, ...] | None = None,
        schedule_slot: str | None = None,
        states: tuple[TaskState, ...] = (TaskState.QUEUED,),
    ) -> Task | None:
        """Take the oldest waiting task, if any, and mark it RUNNING.

        Naming a slot claims that slot and nothing else: a run for this week
        must never pick up the task the scheduler queued for another week and
        write that week's report under this week's numbers.

        `states` is normally just QUEUED. A caller retrying a week whose run
        failed passes FAILED as well: nothing was completed, so there is
        nothing to supersede and no reason to demand `--again`. Attempts are
        still counted, and a task out of attempts is not claimed.
        """
        clause = "AND kind = ANY(%s)" if kinds else ""
        slot_clause = "AND schedule_slot = %s" if schedule_slot is not None else ""
        params: list[Any] = [self.entity_id, [state.value for state in states]]
        if kinds:
            params.append(list(kinds))
        if schedule_slot is not None:
            params.append(schedule_slot)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {COLUMNS} FROM tasks
                 WHERE entity_id = %s AND state = ANY(%s) {clause} {slot_clause}
                   AND attempts < max_attempts
                 ORDER BY created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
            if row is None:
                return None
            task = _row_to_task(row)
            intent = self.audit.intent(
                "task.state",
                {"from": task.state.value, "to": TaskState.RUNNING.value},
                task_id=task.id,
            )
            cur.execute(
                f"""
                UPDATE tasks
                   SET state = 'RUNNING', attempts = attempts + 1,
                       heartbeat_at = now(), updated_at = now()
                 WHERE id = %s
                RETURNING {COLUMNS}
                """,
                (task.id,),
            )
            updated = cur.fetchone()
            assert updated is not None
        claimed = _row_to_task(updated)
        self.audit.outcome(intent, {"attempts": claimed.attempts}, task_id=claimed.id)
        return claimed

    def reopen(self, kind: str, schedule_slot: str, reason: str) -> Task | None:
        """Put a finished slot back in the queue so it can be run again.

        This is the deliberate act behind `shannon run --again`, and it is
        deliberately narrow: it does not touch action fingerprints, so
        anything already staged or sent stays staged or sent exactly once.
        Returns None when there is no finished run of that slot to repeat.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {COLUMNS} FROM tasks
                 WHERE entity_id = %s AND kind = %s AND schedule_slot = %s
                   AND state <> 'RUNNING'
                 FOR UPDATE SKIP LOCKED
                """,
                (self.entity_id, kind, schedule_slot),
            )
            row = cur.fetchone()
        if row is None:
            return None
        task = _row_to_task(row)
        if task.state is TaskState.QUEUED:
            return task
        self._finish(task, TaskState.QUEUED, None, {"reopened_because": reason})
        return self.get(task.id)

    def heartbeat(self, task: Task) -> None:
        self.conn.execute(
            "UPDATE tasks SET heartbeat_at = now(), updated_at = now() WHERE id = %s",
            (task.id,),
        )

    def succeed(self, task: Task, detail: dict[str, Any] | None = None) -> None:
        self._finish(task, TaskState.SUCCEEDED, None, detail)

    def fail(self, task: Task, error: str, detail: dict[str, Any] | None = None) -> None:
        self._finish(task, TaskState.FAILED, error, detail)

    def requeue(self, task: Task, error: str) -> None:
        """A retryable failure with attempts remaining goes back to the queue."""
        self._finish(task, TaskState.QUEUED, error, None)

    def _finish(
        self,
        task: Task,
        state: TaskState,
        error: str | None,
        detail: dict[str, Any] | None,
    ) -> None:
        intent = self.audit.intent(
            "task.state",
            {"from": task.state.value, "to": state.value, "error": error},
            task_id=task.id,
        )
        self.conn.execute(
            "UPDATE tasks SET state = %s, error = %s, updated_at = now() WHERE id = %s",
            (state.value, error, task.id),
        )
        self.audit.outcome(intent, detail or {}, task_id=task.id)

    def reap_stalled(self, grace_seconds: int = 900) -> list[Task]:
        """Recover tasks whose worker died: requeue them, or fail them if they
        have used up their attempts. A task never sits in RUNNING forever."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {COLUMNS} FROM tasks
                 WHERE entity_id = %s AND state = 'RUNNING'
                   AND heartbeat_at < now() - make_interval(secs => %s)
                 FOR UPDATE SKIP LOCKED
                """,
                (self.entity_id, grace_seconds),
            )
            rows = cur.fetchall()
        reaped: list[Task] = []
        for row in rows:
            task = _row_to_task(row)
            message = (
                f"No sign of life for over {grace_seconds} seconds; the process running it stopped."
            )
            if task.attempts >= task.max_attempts:
                self.fail(task, message + " No attempts left.")
            else:
                self.requeue(task, message)
            reaped.append(task)
        return reaped

    def get(self, task_id: str) -> Task | None:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
        return _row_to_task(row) if row is not None else None


def schedule_slot(kind: str, when: datetime | None = None) -> str:
    """The business occurrence a task belongs to — a week, not an attempt."""
    moment = when or datetime.now(tz=UTC)
    year, week, _ = moment.isocalendar()
    return f"{kind}/{year}-W{week:02d}"


__all__ = ["TERMINAL_STATES", "Task", "TaskQueue", "TaskState", "schedule_slot"]
