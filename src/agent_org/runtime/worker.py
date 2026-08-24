"""Wiring: the one place where Shannon, the broker and the executors meet.

Shannon cannot reach an executor herself — an import-linter contract stops
her importing the package. This module does the introductions, so the
doorway stays a doorway.

The order at the end of a run is the durability rule, written out:
the report row and the file commit first, and only then is the email
attempted. A mail server having a bad minute can therefore lose the
delivery and nothing else, and it says so loudly instead of failing the
week.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg

from agent_org.audit.log import AuditLog
from agent_org.broker.broker import ActionBroker, BrokerRefusal
from agent_org.broker.executors.internal_report import (
    ReportEmailer,
    ReportWriter,
    build_registry,
    report_email_executor,
)
from agent_org.broker.registry import ExecutorRegistry
from agent_org.config.models import ComponentKey, LoadedConfig
from agent_org.integrations.gmail import GmailFixtureClient, GmailLiveClient
from agent_org.integrations.reads import InventoryReader, OrderSignalReader, ReadFailure
from agent_org.integrations.veeqo import VeeqoFixtureClient, VeeqoLiveClient
from agent_org.notify.email import Sender, SendFailed, SmtpSender
from agent_org.policy.engine import PolicyEngine
from agent_org.shannon.calculator import ManualProposal
from agent_org.shannon.config_diff import ConfigSnapshot
from agent_org.shannon.run import RunOutcome, Shannon, email_the_report
from agent_org.tasks.budget import BudgetExceeded
from agent_org.tasks.queue import Task, TaskQueue, TaskState, schedule_slot

SHANNON_REPLENISHMENT = "shannon_replenishment"
# An obviously-fake channel name is left in configuration until the live
# account says what Veeqo really calls each channel. Running against it
# would silently mis-split FBA and merchant demand, so it is refused.
PLACEHOLDER = "TBD-"


class RunAlreadyDone(RuntimeError):
    """This week's run has already happened. Re-running would double-count it."""


@dataclass(frozen=True)
class RunSummary:
    task: Task
    outcome: RunOutcome | None
    error: str | None
    # The report is written before the email is attempted, so these are
    # separate facts and are reported separately. A failed send never turns
    # a completed week into a failed one.
    emailed_to: tuple[str, ...] = ()
    email_error: str | None = None
    email_subject: str | None = None

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
    """Saved exports. Same interfaces the live clients use; no credential is
    loaded, because none is needed and none is wanted."""
    return VeeqoFixtureClient(fixture_dir=fixtures), GmailFixtureClient(fixture_dir=fixtures)


def channel_keys_from(config: LoadedConfig) -> dict[str, str]:
    """Veeqo's own spelling of each channel, mapped to our channel key.

    Refused rather than guessed. Veeqo splits sales by whatever name the
    account gives a channel, and which of those names means FBA is the
    answer that decides whether stock is sent to Amazon. Configuration
    carries visible placeholders until someone reads the real names off the
    account, and this stops a run that still holds one.
    """
    keys: dict[str, str] = {}
    unresolved: list[str] = []
    for channel in config.entity.channels:
        name = (channel.veeqo_channel or "").strip()
        if not name:
            unresolved.append(f"{channel.key} (nothing configured)")
        elif name.startswith(PLACEHOLDER):
            unresolved.append(f"{channel.key} (still {name})")
        else:
            keys[name] = channel.key
    if unresolved:
        raise ReadFailure(
            "Veeqo splits sales by the name it prints on each channel, and this "
            "configuration does not yet say what those names are for: "
            + "; ".join(unresolved)
            + ". Shannon will not guess which channel is FBA, because that answer is "
            "what sends stock to Amazon. Open Veeqo, read the channel names exactly "
            "as they are spelled, put them under 'channels:' as 'veeqo_channel', and "
            "run again. Until then use --fixtures."
        )
    return keys


def live_readers(
    config: LoadedConfig, today: date | None = None
) -> tuple[InventoryReader, OrderSignalReader]:
    """The real Veeqo account and the real mailbox, both read-only.

    Credentials are read from the environment inside the clients, at the
    moment of use. Nothing here holds one, and neither client has a method
    that could write to either system.
    """
    prefix = config.entity.credentials_prefix
    return (
        VeeqoLiveClient(
            channel_keys=channel_keys_from(config), credentials_prefix=prefix, today=today
        ),
        GmailLiveClient(credentials_prefix=prefix),
    )


def manual_proposals(
    conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> dict[ComponentKey, ManualProposal]:
    """What has already been proposed against each hand count.

    The count on a shelf does not fall when the shelf is used, so without
    this every hand-counted part would be proposed again every Monday until
    Zach stopped reading the report. The latest proposal per part is enough:
    suppression is decided by whether its count date is still the current
    one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (supplier, part)
                   supplier, part, counted_on, count_units, proposed_units, proposed_on
              FROM manual_stock_proposals
             WHERE entity_id = %s
             ORDER BY supplier, part, counted_on DESC, created_at DESC
            """,
            (entity_id,),
        )
        rows = cur.fetchall()
    proposals: dict[ComponentKey, ManualProposal] = {}
    for row in rows:
        key = ComponentKey(supplier=str(row[0]), part=str(row[1]))
        counted_on = row[2]
        proposed_on = row[5]
        assert isinstance(counted_on, date)
        assert isinstance(proposed_on, date)
        proposals[key] = ManualProposal(
            key=key,
            counted_on=counted_on,
            count=int(str(row[3])),
            units=int(str(row[4])),
            proposed_on=proposed_on,
        )
    return proposals


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
    fixtures: Path | None,
    output_dir: Path,
    now: datetime | None = None,
    again: bool = False,
) -> RunSummary:
    """Claim (or create) this week's task and run it to a written report.

    A week whose run failed is claimed normally: nothing was completed, so
    there is nothing to supersede and no reason to make Zach type a flag.
    `again` is for the other case — a week that finished, whose report is
    regenerated and supersedes the old one. Nothing outside this machine is
    repeated: the broker keeps the fingerprint of any action above Tier 0
    exactly as it was.

    `fixtures` of None means the live Veeqo account and the live mailbox.

    Nothing is emailed here. Delivery is `deliver_report`, called by the
    caller after this transaction has committed — which is the whole point
    of it being a separate function.
    """
    moment = now or datetime.now(tz=UTC)
    entity_id = config.entity_id
    audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
    queue = TaskQueue(conn=conn, entity_id=entity_id, audit=audit)
    slot = schedule_slot(SHANNON_REPLENISHMENT, moment)
    queue.enqueue(SHANNON_REPLENISHMENT, slot)
    # QUEUED or FAILED: a week that was attempted and did not finish is not
    # a week that has been done. The guard below keys on completion.
    task = queue.claim(
        (SHANNON_REPLENISHMENT,),
        schedule_slot=slot,
        states=(TaskState.QUEUED, TaskState.FAILED),
    )
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
            "Its report is in the reports folder and in the database, and it has "
            "been emailed. Run `shannon run --again` to work the week out afresh "
            "and replace that report; nothing is ordered either way."
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
    try:
        inventory, orders = (
            fixture_readers(fixtures)
            if fixtures is not None
            else live_readers(config, moment.date())
        )
    except ReadFailure as exc:
        queue.fail(task, str(exc))
        return RunSummary(task=task, outcome=None, error=str(exc))

    shannon = Shannon(
        config=config,
        inventory=inventory,
        orders=orders,
        broker=broker,
        now=moment,
        manual_proposals=manual_proposals(conn, entity_id),
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


def deliver_report(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    summary: RunSummary,
    sender: Sender | None = None,
) -> RunSummary:
    """Email a report that is already written, and record the attempt.

    Called in its own transaction, after the run's transaction has
    committed and the file is on the disk. That order is the point: a send
    that fails leaves a completed week and a recorded failure, never a lost
    report. The failure comes back on the summary rather than as an
    exception, because the run did not fail — the delivery did.
    """
    outcome = summary.outcome
    if outcome is None:
        return summary
    entity_id = config.entity_id
    identity = config.shannon
    audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
    registry = ExecutorRegistry()
    registry.register(
        report_email_executor(
            ReportEmailer(
                conn=conn,
                entity_id=entity_id,
                sender=sender or SmtpSender(credentials_prefix=config.entity.credentials_prefix),
                from_name=identity.from_name,
                from_address=identity.from_address,
            )
        )
    )
    broker = ActionBroker(
        conn=conn,
        entity_id=entity_id,
        policy=PolicyEngine(config.policy),
        registry=registry,
        audit=audit,
        suppliers=config.boms.suppliers,
    )
    slot = summary.task.schedule_slot
    week = slot.split("/")[-1]
    try:
        email = email_the_report(
            broker=broker,
            config=config,
            outcome=outcome,
            task_id=summary.task.id,
            schedule_slot=slot,
            week=week,
            # A regenerated week is a new report, and the new report is the
            # one worth reading, so it is delivered rather than suppressed
            # as a duplicate of the first send.
            attempt_salt=(f"attempt-{summary.task.attempts}" if summary.task.attempts > 1 else ""),
        )
    except (SendFailed, BrokerRefusal) as exc:
        return replace(summary, email_error=str(exc))
    if email is None:
        return summary
    return replace(
        summary,
        emailed_to=config.shannon.report_email_addresses(),
        email_subject=str(email.result.get("subject") or ""),
    )


__all__ = [
    "SHANNON_REPLENISHMENT",
    "RunAlreadyDone",
    "RunSummary",
    "channel_keys_from",
    "deliver_report",
    "fixture_readers",
    "live_readers",
    "manual_proposals",
    "previous_snapshot",
    "run_replenishment",
]
