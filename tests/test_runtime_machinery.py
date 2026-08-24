"""Write-ahead tasks, append-only audit, fingerprints and budgets."""

from __future__ import annotations

import time

import psycopg
import pytest

from agent_org.broker.broker import fingerprint
from agent_org.runtime.budgets import BudgetExceededError, TaskBudget
from agent_org.runtime.tasks import claim, enqueue, finish
from agent_org.tenancy.session import entity_session

SLOT = "2026-08-24"


def _audit_rows(conn: psycopg.Connection, entity_id: str) -> list[tuple[str, str, str]]:
    with entity_session(conn, entity_id):
        return [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT event, phase, actor FROM audit_log ORDER BY at, id"
            ).fetchall()
        ]


def test_a_task_is_recorded_before_it_runs_and_updated_after(
    clean_db: psycopg.Connection,
) -> None:
    task_id = enqueue(clean_db, "ithrive", "shannon.replenishment", SLOT)
    assert task_id is not None
    with entity_session(clean_db, "ithrive"):
        state = clean_db.execute("SELECT state FROM tasks WHERE id = %s", (task_id,)).fetchone()
    assert state == ("QUEUED",)

    task = claim(clean_db, "ithrive", "shannon.replenishment")
    assert task is not None
    with entity_session(clean_db, "ithrive"):
        row = clean_db.execute(
            "SELECT state, attempts FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    assert row == ("RUNNING", 1)

    finish(clean_db, task, state="SUCCEEDED")
    events = _audit_rows(clean_db, "ithrive")
    assert events == [
        ("task.state", "intent", "scheduler"),
        ("task.state", "intent", "worker"),
        ("task.state", "outcome", "worker"),
    ]


def test_a_failed_task_says_so_rather_than_disappearing(clean_db: psycopg.Connection) -> None:
    enqueue(clean_db, "ithrive", "shannon.replenishment", SLOT)
    task = claim(clean_db, "ithrive", "shannon.replenishment")
    assert task is not None
    finish(clean_db, task, state="FAILED", error="Veeqo did not answer.")
    with entity_session(clean_db, "ithrive"):
        row = clean_db.execute(
            "SELECT state, error FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    assert row == ("FAILED", "Veeqo did not answer.")


def test_the_same_slot_cannot_be_queued_twice(clean_db: psycopg.Connection) -> None:
    assert enqueue(clean_db, "ithrive", "shannon.replenishment", SLOT) is not None
    assert enqueue(clean_db, "ithrive", "shannon.replenishment", SLOT) is None


def test_claiming_an_empty_queue_returns_nothing(clean_db: psycopg.Connection) -> None:
    assert claim(clean_db, "ithrive", "shannon.replenishment") is None


def test_the_audit_log_cannot_be_edited_or_deleted(
    clean_db: psycopg.Connection, app_db: psycopg.Connection
) -> None:
    """Append-only is a database grant, not a convention."""
    enqueue(app_db, "ithrive", "shannon.replenishment", SLOT)
    app_db.commit()
    for statement in (
        "UPDATE audit_log SET actor = 'someone else'",
        "DELETE FROM audit_log",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), entity_session(app_db, "ithrive"):
            app_db.execute(statement)
        app_db.rollback()


def test_a_fingerprint_is_stable_across_runs_and_key_order() -> None:
    a = fingerprint("ithrive", "internal.write_draft_report", {"x": 1, "y": [2, 3]}, SLOT)
    b = fingerprint("ithrive", "internal.write_draft_report", {"y": [2, 3], "x": 1}, SLOT)
    assert a == b


def test_a_fingerprint_changes_with_entity_action_payload_or_slot() -> None:
    base = fingerprint("ithrive", "internal.write_draft_report", {"x": 1}, SLOT)
    assert base != fingerprint("lima_zulu", "internal.write_draft_report", {"x": 1}, SLOT)
    assert base != fingerprint("ithrive", "nar.stage_cart", {"x": 1}, SLOT)
    assert base != fingerprint("ithrive", "internal.write_draft_report", {"x": 2}, SLOT)
    assert base != fingerprint("ithrive", "internal.write_draft_report", {"x": 1}, "2026-08-31")


def test_a_step_budget_stops_a_runaway_task() -> None:
    budget = TaskBudget(wall_seconds=60.0, max_steps=2)
    budget.step("one")
    budget.step("two")
    with pytest.raises(BudgetExceededError) as excinfo:
        budget.step("three")
    assert "step" in str(excinfo.value).lower()


def test_a_time_budget_stops_a_hung_task() -> None:
    budget = TaskBudget(wall_seconds=0.05, max_steps=100)
    budget.step("one")
    time.sleep(0.06)
    with pytest.raises(BudgetExceededError):
        budget.step("two")


def test_the_budget_keeps_a_readable_transcript() -> None:
    budget = TaskBudget(wall_seconds=60.0, max_steps=5)
    budget.step("read veeqo")
    budget.step("calculate")
    assert budget.steps_taken == 2
    assert budget.step_log == ["read veeqo", "calculate"]
