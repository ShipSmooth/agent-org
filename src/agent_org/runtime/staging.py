"""Wiring for a cart-staging run: the report, the plan, the cart, the email.

Kept apart from the weekly run on purpose. Staging acts on a week that has
already been calculated, reported and emailed — it never calculates
anything — so it reads the live report row for that week and stages what
that report says. If the numbers are wrong, the fix is `shannon run
--again`, not a different cart.

Its own task, its own schedule slot, its own report row. A staging report
must never supersede the week's replenishment report: they answer
different questions and Zach reads both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from agent_org.audit.log import AuditLog
from agent_org.broker.broker import ActionBroker, BrokerRefusal
from agent_org.broker.executors.internal_report import (
    ACTION_EMAIL_REPORT,
    ACTION_WRITE_REPORT,
    ReportEmailer,
    ReportWriter,
    build_registry,
)
from agent_org.broker.executors.supplier_cart import (
    ACTION_PLAN_CART_STAGING,
    ACTION_STAGE_CART,
    CartStager,
    plan_cart_staging_executor,
    stage_cart_executor,
)
from agent_org.config.models import LoadedConfig
from agent_org.integrations.carts import CartRefusal, CartUnavailable, SupplierCart
from agent_org.integrations.nar import NarCartClient, NarFixtureCart
from agent_org.notify.email import Sender, SendFailed, SmtpSender
from agent_org.policy.engine import ActionContext, PolicyEngine, TrailingHistory
from agent_org.shannon.config_diff import ConfigSnapshot
from agent_org.shannon.staging import StagingPlan, plan_from_report_lines
from agent_org.shannon.staging_report import StagingContext, render, subject_line
from agent_org.tasks.queue import TaskQueue, TaskState, schedule_slot

SHANNON_CART_STAGING = "shannon_cart_staging"


class NothingToStage(RuntimeError):
    """There is no reported week to act on, or nothing in it for this cart."""


@dataclass(frozen=True)
class StagingSummary:
    supplier: str
    week: str
    plan: StagingPlan
    dry_run: bool
    report_path: str | None = None
    staged: int = 0
    failed: int = 0
    emailed_to: tuple[str, ...] = ()
    email_subject: str | None = None
    email_error: str | None = None
    error: str | None = None


def reported_lines(
    conn: psycopg.Connection[tuple[object, ...]], entity_id: str, slot: str
) -> tuple[list[dict[str, object]], str, str]:
    """This week's replenishment report, as the database has it.

    The live row only: a report a re-run has superseded is not what Zach
    was sent, and staging what he was never shown is exactly the surprise
    this system is built to avoid.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.lines, r.bom_version, r.config_digest
              FROM reports r JOIN tasks t ON t.id = r.task_id
             WHERE r.entity_id = %s AND r.kind = 'replenishment'
               AND t.schedule_slot = %s AND r.superseded_by IS NULL
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
            (entity_id, slot),
        )
        row = cur.fetchone()
    if row is None:
        raise NothingToStage(
            f"There is no replenishment report for {slot}, so there is nothing to "
            "stage. Run `shannon run` first — staging acts on a week that has "
            "already been worked out and reported, and never calculates its own."
        )
    lines = row[0] if isinstance(row[0], list) else json.loads(str(row[0]))
    return list(lines), str(row[1]), str(row[2])


def staging_history(
    conn: psycopg.Connection[tuple[object, ...]], entity_id: str, supplier: str
) -> TrailingHistory:
    """What past live weeks put in this cart — the yardstick for "normal".

    The anomaly escalations need something to compare a week against, and
    for staging that is the weeks already staged, not purchase orders: no
    order has ever been placed through this system. Until there are enough
    of them, policy escalates to Tier 3 and says so, which is the honest
    answer to "is this week unusual?" when there is nothing to be unusual
    against.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT schedule_slot, SUM(units)::float
              FROM cart_stagings
             WHERE entity_id = %s AND supplier = %s AND mode = 'LIVE' AND status = 'ADDED'
             GROUP BY schedule_slot
            """,
            (entity_id, supplier),
        )
        weeks = cur.fetchall()
    if not weeks:
        return TrailingHistory()
    totals = [float(str(row[1] or 0)) for row in weeks]
    return TrailingHistory(
        order_count=len(totals),
        average_total_units=sum(totals) / len(totals),
    )


def nar_cart(fixtures: Path | None, config: LoadedConfig) -> SupplierCart:
    """The live narescue.com cart, or a saved copy of one."""
    if fixtures is not None:
        return NarFixtureCart(fixture_dir=fixtures)
    return NarCartClient(credentials_prefix=config.entity.credentials_prefix)


def stage_supplier_cart(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    supplier: str,
    output_dir: Path,
    fixtures: Path | None = None,
    dry_run: bool = True,
    week: str | None = None,
    now: datetime | None = None,
    cart: SupplierCart | None = None,
) -> StagingSummary:
    """Stage — or rehearse staging — one supplier's cart for one week.

    Nothing is emailed here; delivery is `deliver_staging_report`, after
    this transaction has committed, for the same reason the weekly report
    is delivered separately: a mail server having a bad minute must not
    make a staged cart look unstaged.
    """
    moment = now or datetime.now(tz=UTC)
    entity_id = config.entity_id
    replenishment_slot = (
        f"shannon_replenishment/{week}" if week else schedule_slot("shannon_replenishment", moment)
    )
    week_name = replenishment_slot.split("/")[-1]
    slot = f"{SHANNON_CART_STAGING}/{supplier}/{week_name}"

    lines, bom_version, config_digest = reported_lines(conn, entity_id, replenishment_slot)
    plan = plan_from_report_lines(lines, supplier)

    audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
    queue = TaskQueue(conn=conn, entity_id=entity_id, audit=audit)
    queue.enqueue(SHANNON_CART_STAGING, slot)
    task = queue.claim(
        (SHANNON_CART_STAGING,),
        schedule_slot=slot,
        states=(TaskState.QUEUED, TaskState.FAILED, TaskState.SUCCEEDED),
    )
    if task is None:
        raise NothingToStage(
            f"A staging run for {slot} could not be claimed — one may be running "
            "right now, or this week has used its attempts. Nothing was staged."
        )

    supplier_cart = cart or nar_cart(fixtures, config)
    stager = CartStager(
        conn=conn,
        entity_id=entity_id,
        supplier=supplier,
        cart=supplier_cart,
        dry_run=dry_run,
    )
    registry = build_registry(ReportWriter(conn=conn, entity_id=entity_id, output_dir=output_dir))
    registry.register(
        plan_cart_staging_executor(stager) if dry_run else stage_cart_executor(stager)
    )
    broker = ActionBroker(
        conn=conn,
        entity_id=entity_id,
        policy=PolicyEngine(config.policy),
        registry=registry,
        audit=audit,
        suppliers=config.boms.suppliers,
    )

    summary = StagingSummary(supplier=supplier, week=week_name, plan=plan, dry_run=dry_run)
    try:
        outcome = broker.submit(
            action_type=ACTION_PLAN_CART_STAGING if dry_run else ACTION_STAGE_CART,
            payload={
                "task_id": task.id,
                "schedule_slot": slot,
                "supplier": supplier,
                "lines": [
                    {
                        "sku": line.sku,
                        "name": line.name,
                        "quantity": line.quantity,
                        "units": line.units,
                    }
                    for line in plan.lines
                ],
            },
            task_id=task.id,
            schedule_slot=slot,
            # A dry run keeps the executor's own context: it is internal and
            # reversible, and there is nothing about the size of it for
            # policy to find unusual. A live run is measured.
            context=None
            if dry_run
            else ActionContext(
                reversible="window",
                category="purchase",
                total_units=sum(line.units for line in plan.lines),
                line_quantities=tuple(line.quantity for line in plan.lines),
            ),
            history=None if dry_run else staging_history(conn, entity_id, supplier),
        )
    except (BrokerRefusal, CartUnavailable, CartRefusal) as exc:
        queue.fail(task, str(exc))
        return replace(summary, error=str(exc))

    staged = [line for line in outcome.result["lines"] if line["status"] in ("ADDED", "PLANNED")]
    failed = [line for line in outcome.result["lines"] if line["status"] == "FAILED"]
    supplier_name = _supplier_name(config, supplier)
    body = render(
        plan,
        outcome.result,
        StagingContext(
            entity_name=config.entity.legal_name,
            supplier_name=supplier_name,
            week=week_name,
            generated_at=moment,
        ),
    )
    filename = f"cart-{supplier}-{'dry-run-' if dry_run else ''}{moment:%Y-%m-%d}-{entity_id}.txt"
    written = broker.submit(
        action_type=ACTION_WRITE_REPORT,
        payload={
            "task_id": task.id,
            "kind": "cart_staging",
            "filename": filename,
            "body": body,
            "bom_version": bom_version,
            "config_digest": config_digest,
            "parameters": ConfigSnapshot.of(config).as_dict(),
            "lines": outcome.result["lines"],
        },
        task_id=task.id,
        schedule_slot=slot,
    )
    queue.succeed(task, {"staged": len(staged), "failed": len(failed), "mode": stager.mode})
    return replace(
        summary,
        report_path=str(written.result.get("file_path") or ""),
        staged=len(staged),
        failed=len(failed),
    )


def deliver_staging_report(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    summary: StagingSummary,
    sender: Sender | None = None,
) -> StagingSummary:
    """Email the confirmation report, in its own transaction, once it is filed."""
    if summary.report_path is None:
        return summary
    entity_id = config.entity_id
    slot = f"{SHANNON_CART_STAGING}/{summary.supplier}/{summary.week}"
    report = _staging_report_row(conn, entity_id, slot)
    if report is None:
        return summary
    report_id, task_id = report
    identity = config.shannon
    audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
    registry = build_registry(
        ReportWriter(conn=conn, entity_id=entity_id, output_dir=Path(".")),
        ReportEmailer(
            conn=conn,
            entity_id=entity_id,
            sender=sender or SmtpSender(credentials_prefix=config.entity.credentials_prefix),
            from_name=identity.from_name,
            from_address=identity.from_address,
        ),
    )
    broker = ActionBroker(
        conn=conn,
        entity_id=entity_id,
        policy=PolicyEngine(config.policy),
        registry=registry,
        audit=audit,
        suppliers=config.boms.suppliers,
    )
    recipients = config.shannon.report_email_addresses()
    subject = subject_line(
        supplier_name=_supplier_name(config, summary.supplier),
        week=summary.week,
        staged=summary.staged,
        failed=summary.failed,
        dry_run=summary.dry_run,
    )
    try:
        broker.submit(
            action_type=ACTION_EMAIL_REPORT,
            payload={
                "report_id": report_id,
                "recipients": list(recipients),
                "subject": subject,
            },
            task_id=task_id,
            schedule_slot=slot,
        )
    except (SendFailed, BrokerRefusal) as exc:
        return replace(summary, email_error=str(exc), email_subject=subject)
    return replace(summary, emailed_to=recipients, email_subject=subject)


def _staging_report_row(
    conn: psycopg.Connection[tuple[object, ...]], entity_id: str, slot: str
) -> tuple[str, str] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.task_id
              FROM reports r JOIN tasks t ON t.id = r.task_id
             WHERE r.entity_id = %s AND r.kind = 'cart_staging'
               AND t.schedule_slot = %s AND r.superseded_by IS NULL
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
            (entity_id, slot),
        )
        row = cur.fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None


def _supplier_name(config: LoadedConfig, supplier: str) -> str:
    found = config.boms.suppliers.get(supplier)
    return found.name if found is not None else supplier


__all__ = [
    "SHANNON_CART_STAGING",
    "NothingToStage",
    "StagingSummary",
    "deliver_staging_report",
    "nar_cart",
    "reported_lines",
    "stage_supplier_cart",
]
