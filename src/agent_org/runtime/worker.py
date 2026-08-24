"""Wiring: the one place where Shannon, the broker and the executors meet.

Shannon cannot reach an executor herself — an import-linter contract stops
her importing the package. This module does the introductions, so the
doorway stays a doorway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from agent_org.audit.log import AuditLog
from agent_org.broker.broker import ActionBroker, BrokerRefusal
from agent_org.broker.executors.internal_report import ReportWriter, build_registry
from agent_org.config.models import LoadedConfig
from agent_org.integrations.gmail import GmailFixtureClient
from agent_org.integrations.reads import InventoryReader, OrderSignalReader, ReadFailure
from agent_org.integrations.veeqo import VeeqoFixtureClient
from agent_org.policy.engine import PolicyEngine
from agent_org.shannon.config_diff import ConfigSnapshot
from agent_org.shannon.run import RunOutcome, Shannon
from agent_org.tasks.budget import BudgetExceeded
from agent_org.tasks.queue import Task, TaskQueue, schedule_slot

SHANNON_REPLENISHMENT = "shannon_replenishment"


class RunAlreadyDone(RuntimeError):
    """This week's run has already happened. Re-running would double-count it."""


@dataclass(frozen=True)
class RunSummary:
    task: Task
    outcome: RunOutcome | None
    error: str | None

    @property
    def report_path(self) -> str | None:
        return self._result("file_path")

    @property
    def superseded_report_id(self) -> str | None:
        """The report this run replaced, when it was a re-run of a week."""
        return self._result("supersedes")

    @property
    def superseded_written_at(self) -> str | None:
        return self._result("supersedes_written_at")

    @property
    def superseded_path(self) -> str | None:
        return self._result("supersedes_file_path")

    def _result(self, key: str) -> str | None:
        if self.outcome is None or self.outcome.broker_outcome is None:
            return None
        value = self.outcome.broker_outcome.result.get(key)
        return str(value) if value else None


def fixture_readers(fixtures: Path) -> tuple[InventoryReader, OrderSignalReader]:
    """Phase 1 reads from saved exports. Same interfaces the live clients use;
    no credential is loaded, because none is needed and none is wanted."""
    return VeeqoFixtureClient(fixture_dir=fixtures), GmailFixtureClient(fixture_dir=fixtures)


def previous_snapshot(
    conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> ConfigSnapshot | None:
    """The configuration the last report was written against.

    Superseded reports are excluded: a re-run compares against the week
    that was actually reported, not against the copy it just replaced,
    which would always show "nothing changed".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT parameters FROM reports
             WHERE entity_id = %s AND kind = 'replenishment'
               AND superseded_by IS NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (entity_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    data = row[0] if isinstance(row[0], dict) else json.loads(str(row[0]))
    return ConfigSnapshot.from_dict(data)


def run_replenishment(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    fixtures: Path,
    output_dir: Path,
    now: datetime | None = None,
    again: bool = False,
) -> RunSummary:
    """Claim (or create) this week's task and run it to a written report.

    With `again`, a week that has already been run is run again: the reading
    and the arithmetic happen a second time and the new report supersedes
    the old one. Nothing outside this machine is repeated — the broker keeps
    the fingerprint of any action above Tier 0 exactly as it was.
    """
    moment = now or datetime.now(tz=UTC)
    entity_id = config.entity_id
    audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
    queue = TaskQueue(conn=conn, entity_id=entity_id, audit=audit)
    slot = schedule_slot(SHANNON_REPLENISHMENT, moment)
    queue.enqueue(SHANNON_REPLENISHMENT, slot)
    task = queue.claim((SHANNON_REPLENISHMENT,), schedule_slot=slot)
    if task is None and again:
        task = queue.reopen(
            SHANNON_REPLENISHMENT,
            slot,
            "`shannon run --again`: report regenerated on request.",
        )
        task = (
            queue.claim((SHANNON_REPLENISHMENT,), schedule_slot=slot) if task is not None else None
        )
    if task is None:
        raise RunAlreadyDone(
            f"This week's replenishment run ({slot}) has already been carried out. "
            "Its report is in the reports folder and in the database. "
            "Run `shannon run --again` to work the week out afresh and replace "
            "that report; nothing is sent or ordered either way."
        )

    registry = build_registry(ReportWriter(conn=conn, entity_id=entity_id, output_dir=output_dir))
    broker = ActionBroker(
        conn=conn,
        entity_id=entity_id,
        policy=PolicyEngine(config.policy),
        registry=registry,
        audit=audit,
        suppliers=config.boms.suppliers,
    )
    inventory, orders = fixture_readers(fixtures)
    shannon = Shannon(
        config=config,
        inventory=inventory,
        orders=orders,
        broker=broker,
        now=moment,
    )

    try:
        outcome = shannon.run(
            task_id=task.id,
            schedule_slot=slot,
            previous_snapshot=previous_snapshot(conn, entity_id),
            # The attempt number, not the week: the week is the business
            # occurrence and stays the fingerprint's key. The broker ignores
            # this for anything above Tier 0.
            attempt_salt=f"attempt-{task.attempts}" if task.attempts > 1 else "",
        )
    except (ReadFailure, BrokerRefusal, BudgetExceeded) as exc:
        queue.fail(task, str(exc))
        return RunSummary(task=task, outcome=None, error=str(exc))

    queue.succeed(
        task,
        {
            "report": outcome.filename,
            "components": len(outcome.result.components),
        },
    )
    return RunSummary(task=task, outcome=outcome, error=None)


__all__ = [
    "SHANNON_REPLENISHMENT",
    "RunAlreadyDone",
    "RunSummary",
    "fixture_readers",
    "previous_snapshot",
    "run_replenishment",
]
