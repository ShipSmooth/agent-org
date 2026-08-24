"""One Shannon replenishment run, end to end — reads, arithmetic, report.

The run is a task: recorded QUEUED before it starts, RUNNING before work
begins, SUCCEEDED/FAILED after — with an audit intent/outcome pair around
every read and the report write. Budgets kill a hung run. The only action
with an effect (writing the report) goes through the ActionBroker as
Tier 0; nothing above Tier 0 can execute in Phase 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import psycopg

from agent_org.broker.broker import ActionBroker
from agent_org.integrations.gmail import GmailReadClient
from agent_org.integrations.veeqo import VeeqoReadClient
from agent_org.runtime.audit import audit
from agent_org.runtime.budgets import TaskBudget
from agent_org.runtime.tasks import Task, claim, enqueue, finish, record_agent_run
from agent_org.shannon.calculator import RunStopped, run_calculation
from agent_org.shannon.config_model import EntityConfig, load_entity_config
from agent_org.shannon.report import render_report
from agent_org.shannon.validate import validate
from agent_org.tenancy.session import entity_session

TASK_KIND = "shannon.replenishment"


@dataclass(frozen=True)
class RunOutcome:
    task_id: str
    report_path: str


def _previous_snapshot(conn: psycopg.Connection, entity_id: str) -> str | None:
    with entity_session(conn, entity_id):
        row = conn.execute(
            "SELECT config_snapshot FROM reports WHERE entity_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
    return str(row[0]) if row else None


def run_replenishment(
    conn: psycopg.Connection,
    broker: ActionBroker,
    *,
    config_dir: Path,
    entity_id: str,
    fixtures_dir: Path,
    out_dir: Path,
    schedule_slot: str,
    wall_seconds: float = 900.0,
    max_steps: int = 50,
) -> RunOutcome:
    cfg = load_entity_config(config_dir, entity_id)
    issues = validate(cfg)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        raise RunStopped(
            "Configuration is invalid — run `shannon validate-config` to see every "
            "problem. First error: " + errors[0].render()
        )

    enqueue(conn, entity_id, TASK_KIND, schedule_slot)
    conn.commit()
    task = claim(conn, entity_id, TASK_KIND, schedule_slot)
    if task is None:
        raise RunStopped(
            f"A run for slot {schedule_slot} already happened (the task exists and "
            "is not queued). Re-running the same slot never duplicates work."
        )
    conn.commit()
    try:
        outcome = _execute(
            conn,
            task,
            cfg,
            broker,
            fixtures_dir=fixtures_dir,
            out_dir=out_dir,
            wall_seconds=wall_seconds,
            max_steps=max_steps,
        )
    except Exception as exc:
        finish(conn, task, state="FAILED", error=str(exc))
        conn.commit()
        raise
    finish(conn, task, state="SUCCEEDED")
    conn.commit()
    return outcome


def _execute(
    conn: psycopg.Connection,
    task: Task,
    cfg: EntityConfig,
    broker: ActionBroker,
    *,
    fixtures_dir: Path,
    out_dir: Path,
    wall_seconds: float,
    max_steps: int,
) -> RunOutcome:
    budget = TaskBudget(wall_seconds=wall_seconds, max_steps=max_steps)
    entity_id = cfg.entity_id

    def audited_read(event: str) -> None:
        with entity_session(conn, entity_id):
            audit(conn, entity_id, actor="shannon", event=event, phase="intent", task_id=task.id)

    def read_done(event: str, detail: dict[str, object]) -> None:
        with entity_session(conn, entity_id):
            audit(
                conn,
                entity_id,
                actor="shannon",
                event=event,
                phase="outcome",
                task_id=task.id,
                detail=detail,
            )

    budget.step("read veeqo")
    audited_read("veeqo.read")
    snap = VeeqoReadClient(fixtures_dir).snapshot()
    read_done("veeqo.read", {"skus": len(snap.stock), "window_days": snap.window_days})

    budget.step("read gmail")
    audited_read("gmail.read_order_signals")
    on_order = GmailReadClient(fixtures_dir).on_order()
    read_done(
        "gmail.read_order_signals",
        {"outstanding_orders": [o.order_number for o in on_order.outstanding]},
    )

    budget.step("calculate")
    result = run_calculation(cfg, snap, on_order)

    budget.step("render report")
    previous = _previous_snapshot(conn, entity_id)
    warnings = [i for i in validate(cfg) if i.level == "warning"]
    content = render_report(
        cfg,
        result,
        schedule_slot=task.schedule_slot,
        warnings=warnings,
        previous_snapshot=previous,
    )

    budget.step("write report")
    proposal = broker.propose(
        conn,
        entity_id=entity_id,
        task_id=task.id,
        action_type="internal.write_draft_report",
        payload={
            "content": content,
            "out_dir": str(out_dir),
            "kind": TASK_KIND,
            "schedule_slot": task.schedule_slot,
            "bom_version": cfg.bom_version,
            "config_snapshot": json.dumps(cfg.config_texts),
            "task_id": task.id,
        },
        schedule_slot=task.schedule_slot,
    )
    if proposal.status != "EXECUTED" or proposal.result is None:
        raise RunStopped(
            f"The report write was not executed (status {proposal.status}) — "
            "the run cannot claim success without its report."
        )
    record_agent_run(
        conn,
        task,
        agent_kind="shannon",
        step_count=budget.steps_taken,
        wall_ms=budget.wall_ms,
        transcript=budget.step_log,
    )
    return RunOutcome(task_id=task.id, report_path=str(proposal.result["file_path"]))
