"""Task states, write-ahead audit, budgets.

The property being tested throughout: the log never claims less than what
happened. Intent goes in before the work, the outcome after, and nothing
is ever edited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from agent_org.audit.log import AuditLog
from agent_org.db.connection import entity_session
from agent_org.tasks.budget import Budget, BudgetExceeded
from agent_org.tasks.queue import TaskQueue, TaskState, schedule_slot


def _audit_rows(
    conn: psycopg.Connection[tuple[object, ...]], task_id: str
) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT phase, event, detail FROM audit_log WHERE task_id = %s ORDER BY id",
            (task_id,),
        )
        return list(cur.fetchall())


def test_the_same_week_is_never_queued_twice(
    app_conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        queue = TaskQueue(conn, entity_id, AuditLog(conn=conn, entity_id=entity_id, actor="test"))
        slot = schedule_slot("shannon_replenishment", datetime(2026, 3, 2, tzinfo=UTC))
        first = queue.enqueue("shannon_replenishment", slot)
        second = queue.enqueue("shannon_replenishment", slot)
        assert first.id == second.id


def test_a_run_for_one_week_never_claims_another_weeks_task(
    app_conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> None:
    """Found by running it: a manual run picked up the scheduler's queued
    weekly task and wrote that week's report under this week's numbers."""
    with entity_session(app_conn, entity_id) as conn:
        queue = TaskQueue(conn, entity_id, AuditLog(conn=conn, entity_id=entity_id, actor="test"))
        scheduled = schedule_slot("shannon_replenishment", datetime(2026, 4, 6, tzinfo=UTC))
        manual = schedule_slot("shannon_replenishment", datetime(2026, 4, 13, tzinfo=UTC))
        queued_by_scheduler = queue.enqueue("shannon_replenishment", scheduled)
        queued_by_hand = queue.enqueue("shannon_replenishment", manual)

        claimed = queue.claim(("shannon_replenishment",), schedule_slot=manual)
        assert claimed is not None
        assert claimed.id == queued_by_hand.id
        assert claimed.schedule_slot == manual

        # The older task is untouched and still waiting for its own run.
        still_queued = queue.get(queued_by_scheduler.id)
        assert still_queued is not None
        assert still_queued.state is TaskState.QUEUED

        # Naming a slot with nothing queued for it claims nothing at all,
        # rather than falling back to whatever is oldest.
        empty = schedule_slot("shannon_replenishment", datetime(2026, 4, 20, tzinfo=UTC))
        assert queue.claim(("shannon_replenishment",), schedule_slot=empty) is None

        queue.succeed(claimed, {"report": "manual.md"})


def test_intent_is_written_before_the_state_it_describes(
    app_conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        queue = TaskQueue(conn, entity_id, AuditLog(conn=conn, entity_id=entity_id, actor="test"))
        slot = schedule_slot("shannon_replenishment", datetime(2026, 3, 9, tzinfo=UTC))
        queue.enqueue("shannon_replenishment", slot)
        task = queue.claim(("shannon_replenishment",))
        assert task is not None
        assert task.state is TaskState.RUNNING
        queue.succeed(task, {"report": "somefile.md"})

        phases = [(row[0], row[1]) for row in _audit_rows(conn, task.id)]
        state_changes = [pair for pair in phases if pair[1] == "task.state"]
        assert state_changes == [
            ("intent", "task.state"),  # about to claim it
            ("outcome", "task.state"),  # claimed
            ("intent", "task.state"),  # about to finish it
            ("outcome", "task.state"),  # finished
        ]

        stored = queue.get(task.id)
        assert stored is not None
        assert stored.state is TaskState.SUCCEEDED


def test_the_audit_log_cannot_be_rewritten(
    app_conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> None:
    """Append-only is a grant, not a convention."""
    with entity_session(app_conn, entity_id) as conn:
        audit = AuditLog(conn=conn, entity_id=entity_id, actor="test")
        entry = audit.intent("test.event", {"note": "as recorded"})
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET detail = '{}' WHERE id = %s", (entry.id,))
    app_conn.rollback()


def test_a_failure_keeps_its_reason(
    app_conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        queue = TaskQueue(conn, entity_id, AuditLog(conn=conn, entity_id=entity_id, actor="test"))
        slot = schedule_slot("shannon_replenishment", datetime(2026, 3, 16, tzinfo=UTC))
        queue.enqueue("shannon_replenishment", slot)
        task = queue.claim(("shannon_replenishment",))
        assert task is not None
        queue.fail(task, "Gmail could not be read, so on-order is unknown.")
        stored = queue.get(task.id)
        assert stored is not None
        assert stored.state is TaskState.FAILED
        assert stored.error is not None
        assert "Gmail" in stored.error


def test_a_hung_task_is_reaped_and_says_why(
    app_conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        queue = TaskQueue(conn, entity_id, AuditLog(conn=conn, entity_id=entity_id, actor="test"))
        slot = schedule_slot("shannon_replenishment", datetime(2026, 3, 23, tzinfo=UTC))
        queue.enqueue("shannon_replenishment", slot, max_attempts=1)
        task = queue.claim(("shannon_replenishment",))
        assert task is not None
        conn.execute(
            "UPDATE tasks SET heartbeat_at = now() - interval '2 hours' WHERE id = %s",
            (task.id,),
        )
        reaped = queue.reap_stalled(grace_seconds=900)
        assert [item.id for item in reaped] == [task.id]
        stored = queue.get(task.id)
        assert stored is not None
        assert stored.state is TaskState.FAILED
        assert stored.error is not None
        assert "No sign of life" in stored.error


def test_a_step_budget_stops_a_loop() -> None:
    budget = Budget(wall_clock_seconds=1800, max_steps=3)
    for index in range(3):
        budget.step(f"step {index}")
    with pytest.raises(BudgetExceeded) as caught:
        budget.step("one too many")
    assert "Something is looping" in str(caught.value)


def test_a_wall_clock_budget_stops_a_hang() -> None:
    clock = iter([0.0, 0.0, 61.0])
    budget = Budget(wall_clock_seconds=60, max_steps=1000, _clock=lambda: next(clock))
    budget.step("quick")
    with pytest.raises(BudgetExceeded) as caught:
        budget.step("slow")
    assert "60 seconds" in str(caught.value)
