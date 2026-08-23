"""Shannon's weekly replenishment run.

The order of events matters and is fixed here:

1. Validate the configuration. A run on a broken BOM is worse than no run.
2. Read stock, velocity, inbound and outstanding orders. Any source that
   cannot be read cleanly stops the run — no partial arithmetic.
3. Calculate.
4. Render the report and file it as a proposal with the ActionBroker.

Shannon never writes the file herself. She asks; the broker decides; an
executor writes. That is the same doorway a cart or an email will go
through later, so nothing about this run has to change when they arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from agent_org.broker.broker import ActionBroker, BrokerOutcome
from agent_org.config.errors import ConfigError, Severity
from agent_org.config.models import LoadedConfig
from agent_org.config.validate import ValidationResult, validate
from agent_org.integrations.reads import InventoryReader, OrderSignalReader
from agent_org.shannon.calculator import ReplenishmentCalculator, ReplenishmentResult
from agent_org.shannon.config_diff import ConfigSnapshot, describe_changes
from agent_org.shannon.report import ReportContext, render
from agent_org.tasks.budget import Budget

ACTION_WRITE_REPORT = "internal.write_draft_report"


@dataclass(frozen=True)
class RunOutcome:
    report_body: str
    result: ReplenishmentResult
    validation: ValidationResult
    snapshot: ConfigSnapshot
    broker_outcome: BrokerOutcome | None
    filename: str


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
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.orders = orders
        self.broker = broker
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
    ) -> RunOutcome:
        validation = validate(self.config, self.config.findings)
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

        result = ReplenishmentCalculator(
            config=self.config,
            stock=stock,
            velocity=velocity,
            inbound=inbound,
            on_order=signals.on_order,
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
                },
                task_id=task_id,
                schedule_slot=schedule_slot,
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


def _report_lines(result: ReplenishmentResult) -> list[dict[str, str | int | None]]:
    """The report's numbers in structured form, for the database row."""
    lines: list[dict[str, str | int | None]] = []
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
                "on_hand": plan.on_hand,
                "on_order": plan.on_order,
                "in_transit": plan.in_transit,
                "routing": plan.routing,
            }
        )
    return lines


def _exact(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{float(value):.4f}"


__all__ = ["ACTION_WRITE_REPORT", "RunOutcome", "Shannon"]
