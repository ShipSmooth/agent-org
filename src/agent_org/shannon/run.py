"""Shannon's weekly replenishment run.

The order of events matters and is fixed here:

1. Validate the configuration. A run on a broken BOM is worse than no run.
2. Read stock, velocity, inbound and outstanding orders. Any source that
   cannot be read cleanly stops the run — no partial arithmetic, and never
   a zero standing in for a number nobody could read.
3. Calculate.
4. Render the report and file it as a proposal with the ActionBroker.
5. Once that has committed, ask the broker to email it. Separate step,
   separate record: a report that is written but not delivered is a
   different thing from a week that never ran, and both are visible.

Shannon never writes the file or sends the mail herself. She asks; the
broker decides; an executor acts. There is still no executor that can
reach a supplier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from agent_org.broker.broker import ActionBroker, BrokerOutcome
from agent_org.config.errors import ConfigError, Severity
from agent_org.config.models import ComponentKey, LoadedConfig
from agent_org.config.validate import ValidationResult, validate
from agent_org.integrations.reads import (
    HistoricalVelocityReader,
    InventoryReader,
    OrderSignalReader,
)
from agent_org.notify.email import subject_line
from agent_org.shannon.calculator import (
    ManualProposal,
    ReplenishmentCalculator,
    ReplenishmentResult,
)
from agent_org.shannon.config_diff import ConfigSnapshot, describe_changes
from agent_org.shannon.report import ReportContext, render
from agent_org.tasks.budget import Budget

ACTION_WRITE_REPORT = "internal.write_draft_report"
ACTION_EMAIL_REPORT = "internal.email_report_to_owner"


@dataclass(frozen=True)
class RunOutcome:
    report_body: str
    result: ReplenishmentResult
    validation: ValidationResult
    snapshot: ConfigSnapshot
    broker_outcome: BrokerOutcome | None
    filename: str

    @property
    def report_id(self) -> str | None:
        if self.broker_outcome is None:
            return None
        value = self.broker_outcome.result.get("report_id")
        return str(value) if value else None

    @property
    def lines_needing_an_order(self) -> int:
        return sum(1 for plan in self.result.components if plan.order_units > 0)

    @property
    def blocked_lines(self) -> int:
        return len(self.validation.line_errors)


class Shannon:
    """Shannon, the replenishment agent for iThrive Medical."""

    name = "Shannon"
    key = "shannon"

    def __init__(
        self,
        config: LoadedConfig,
        inventory: InventoryReader,
        orders: OrderSignalReader,
        broker: ActionBroker | None = None,
        budget: Budget | None = None,
        now: datetime | None = None,
        manual_proposals: Mapping[ComponentKey, ManualProposal] | None = None,
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.orders = orders
        self.broker = broker
        # What was already proposed against each hand count, read back from
        # the database. Without it every hand-counted part would be proposed
        # again every Monday, because a shelf count does not fall when the
        # shelf does.
        self.manual_proposals = dict(manual_proposals or {})
        self.now = now or datetime.now(UTC)
        params = config.shannon.parameters
        self.budget = budget or Budget(
            wall_clock_seconds=float(params.task_wall_clock_budget_seconds),
            max_steps=params.task_step_budget,
        )

    def run(
        self,
        task_id: str,
        schedule_slot: str,
        previous_snapshot: ConfigSnapshot | None = None,
        attempt_salt: str = "",
    ) -> RunOutcome:
        """Read, calculate, report.

        `attempt_salt` marks a deliberate repeat of a week. It reaches the
        broker, which honours it for writing a report and ignores it for
        anything with an effect outside this machine.
        """
        # Checked against the day being run, not against the clock: a week
        # re-run later must see the configuration as it was judged then,
        # and "that count is dated in the future" has to mean the future of
        # the run.
        validation = validate(self.config, self.config.findings, today=self.now.date())
        self.budget.step("checking the configuration")
        if validation.blocking_errors:
            raise ConfigError(list(validation.blocking_errors))

        stock = self.inventory.read_inventory()
        self.budget.step("reading stock from Veeqo")
        velocity = self.inventory.read_velocity(self.config.shannon.parameters.velocity_window_days)
        self.budget.step("reading sales history from Veeqo")
        inbound = self.inventory.read_fba_inbound()
        self.budget.step("reading inbound Amazon shipments")
        signals = self.orders.read_order_signals()
        self.budget.step("reading outstanding orders from Gmail")
        history = (
            self.inventory.read_velocity_history()
            if isinstance(self.inventory, HistoricalVelocityReader)
            else {}
        )

        result = ReplenishmentCalculator(
            config=self.config,
            stock=stock,
            velocity=velocity,
            inbound=inbound,
            on_order=signals.on_order,
            historical_velocity=history,
            manual_proposals=self.manual_proposals,
            today=self.now.date(),
        ).calculate()
        self.budget.step("working out what to order")

        snapshot = ConfigSnapshot.of(self.config)
        context = ReportContext(
            entity_name=self.config.entity.legal_name,
            generated_at=self.now,
            config_changes=describe_changes(snapshot, previous_snapshot),
            validation_warnings=tuple(
                finding.render()
                for finding in validation.findings
                if finding.severity is Severity.WARNING
            )
            + signals.warnings,
            order_signals=signals,
            blocked=tuple(finding.render() for finding in validation.line_errors),
            data_sources=(
                f"Stock and {self.config.shannon.parameters.velocity_window_days}-day "
                f"sales: Veeqo ({len(stock)} SKUs read)",
                f"Inbound to Amazon: {len(inbound)} shipment(s)",
                "Outstanding orders: Gmail (confirmation with no shipping notice)",
                *_excluded_channels_line(self.config),
            ),
        )
        body = render(result, self.config, context)
        filename = f"replenishment-{self.now:%Y-%m-%d}-{self.config.entity_id}.txt"

        outcome: BrokerOutcome | None = None
        if self.broker is not None:
            outcome = self.broker.submit(
                action_type=ACTION_WRITE_REPORT,
                payload={
                    "task_id": task_id,
                    "kind": "replenishment",
                    "filename": filename,
                    "body": body,
                    "bom_version": result.bom_version,
                    "config_digest": snapshot.digest,
                    "parameters": snapshot.as_dict(),
                    "lines": _report_lines(result),
                    "manual_proposals": _manual_proposals(result),
                },
                task_id=task_id,
                schedule_slot=schedule_slot,
                attempt_salt=attempt_salt,
            )
        self.budget.step("writing the report")

        return RunOutcome(
            report_body=body,
            result=result,
            validation=validation,
            snapshot=snapshot,
            broker_outcome=outcome,
            filename=filename,
        )


def email_the_report(
    broker: ActionBroker,
    config: LoadedConfig,
    outcome: RunOutcome,
    task_id: str,
    schedule_slot: str,
    week: str,
    attempt_salt: str = "",
) -> BrokerOutcome | None:
    """Hand the written report to the people configuration names.

    Deliberately not a method on Shannon and deliberately not part of
    `run()`: it happens after the report has committed to the database and
    landed on disk, in a transaction of its own, so a mail server having a
    bad minute loses the delivery and nothing else. The addresses come from
    roles in this entity's own config; there is none in this file.
    """
    report_id = outcome.report_id
    if report_id is None:
        return None
    recipients = config.shannon.report_email_addresses()
    subject = subject_line(
        week=week,
        lines_needing_an_order=outcome.lines_needing_an_order,
        blocked=outcome.blocked_lines,
    )
    return broker.submit(
        action_type=ACTION_EMAIL_REPORT,
        payload={
            "report_id": report_id,
            "recipients": list(recipients),
            "subject": subject,
        },
        task_id=task_id,
        schedule_slot=schedule_slot,
        attempt_salt=attempt_salt,
    )


def _manual_proposals(result: ReplenishmentResult) -> list[dict[str, str | int]]:
    """This run's proposals against hand counts, for the next run to see."""
    return [
        {
            "supplier": proposal.key.supplier,
            "part": proposal.key.part,
            "counted_on": proposal.counted_on.isoformat(),
            "count": proposal.count,
            "units": proposal.units,
            "proposed_on": proposal.proposed_on.isoformat(),
        }
        for proposal in result.manual_proposals
    ]


def _excluded_channels_line(config: LoadedConfig) -> tuple[str, ...]:
    """The channels whose sales were deliberately not counted.

    Demand that is left out on purpose is still demand left out, so it is
    named on the report rather than being invisible in a config file.
    """
    excluded = config.entity.excluded_veeqo_channels
    if not excluded:
        return ()
    return (
        "Not counted towards demand, by decision: "
        + ", ".join(sorted(excluded))
        + " — reorder demand is US only.",
    )


def _report_lines(result: ReplenishmentResult) -> list[dict[str, str | int | bool | None]]:
    """The report's numbers in structured form, for the database row.

    Read back by cart staging, which acts on exactly these numbers rather
    than working the week out a second time.
    """
    lines: list[dict[str, str | int | bool | None]] = []
    for plan in result.components:
        lines.append(
            {
                "component": str(plan.key),
                "name": plan.name,
                "class": plan.component_class.value,
                "raw_net": _exact(plan.raw_net),
                "net_units": plan.net_units,
                "moq_rounded": plan.moq_rounded,
                "rounded_to_five": plan.order_units,
                "purchase_units": plan.purchase_units,
                "actual_units": plan.actual_units,
                # Kept on the row because staging reads it back: a cart
                # takes purchase units, and a line whose part number is ours
                # rather than the supplier's has no SKU to add at all.
                "units_per_purchase_unit": plan.units_per_purchase_unit,
                "purchase_unit_name": plan.purchase_unit_name,
                "part_is_internal_reference": plan.part_is_internal_reference,
                "on_hand": plan.on_hand,
                "on_order": plan.on_order,
                "in_transit": plan.in_transit,
                "routing": plan.routing,
            }
        )
    return lines


def _exact(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{float(value):.4f}"


__all__ = [
    "ACTION_EMAIL_REPORT",
    "ACTION_WRITE_REPORT",
    "RunOutcome",
    "Shannon",
    "email_the_report",
]
