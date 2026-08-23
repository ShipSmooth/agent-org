"""Shannon's report.

Written to a file and to the database. Not emailed: sending is a Tier 2
action and this phase has no way to send anything.

Every line shows the whole arithmetic — raw need, after the supplier's
minimum, after rounding to a multiple of five, in purchase units, and the
units that actually arrive — because the rounding cost should be visible,
not buried.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction

from agent_org.config.models import ComponentClass, LoadedConfig
from agent_org.integrations.reads import OrderSignals
from agent_org.shannon.calculator import ReplenishmentResult

RULE = "=" * 78
THIN = "-" * 78


def _n(value: Fraction | int) -> str:
    """Print an exact number: 245 rather than 245.0, 34.29 when it is not whole."""
    if isinstance(value, int):
        return str(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{float(value):.2f}"


@dataclass(frozen=True)
class ReportContext:
    entity_name: str
    generated_at: datetime
    config_changes: str
    validation_warnings: tuple[str, ...]
    order_signals: OrderSignals
    data_sources: tuple[str, ...]
    blocked: tuple[str, ...] = ()


def render(result: ReplenishmentResult, config: LoadedConfig, context: ReportContext) -> str:
    lines: list[str] = []
    add = lines.append

    add(RULE)
    add(f"SHANNON — WEEKLY REPLENISHMENT REPORT — {context.entity_name}")
    add(f"Generated {context.generated_at:%A %d %B %Y, %H:%M %Z}")
    add(RULE)
    add("")
    add("PHASE 1 — READ ONLY. Shannon read the numbers, did the arithmetic and wrote this report.")
    add("Nothing was ordered, no cart was staged, no email or text was sent, and nothing outside")
    add("this file was changed. Every quantity below is a recommendation for you.")
    add("")
    add(f"BOM version: {result.bom_version}")
    add(context.config_changes)
    add("")

    add("PARAMETERS USED")
    add(THIN)
    for name, value in sorted(asdict(result.parameters).items()):
        add(f"  {name:<32} {value}")
    add("")

    add("WHERE THE NUMBERS CAME FROM")
    add(THIN)
    for source in context.data_sources:
        add(f"  {source}")
    signals = context.order_signals
    if signals.outstanding_orders:
        add("  Still awaiting shipment (Gmail): " + ", ".join(signals.outstanding_orders))
    else:
        add("  Still awaiting shipment (Gmail): nothing outstanding")
    for flag in signals.split_shipment_flags:
        add(f"  Split shipment: {flag}")
    for directive in signals.ignored_directives:
        add(f"  Ignored instruction found in an email: {directive}")
    add("")

    add("WHAT TO ORDER")
    add(THIN)
    add("  raw need → after supplier minimum → rounded up to 5 → purchase units → units received")
    add("")
    for plan in result.components:
        if plan.component_class is ComponentClass.NON_STOCKED:
            continue
        if plan.order_units <= 0:
            continue
        purchase = "not confirmed" if plan.purchase_units is None else str(plan.purchase_units)
        actual = "—" if plan.actual_units is None else str(plan.actual_units)
        add(f"  {plan.key}  {plan.name}")
        add(
            f"      {_n(plan.raw_net)} → {plan.moq_rounded} → {plan.order_units} → "
            f"{purchase} → {actual}"
            + (f"   ({plan.purchase_unit_name})" if plan.purchase_unit_name else "")
        )
        add(
            f"      demand {_n(plan.gross_demand)} = standalone {_n(plan.standalone_demand)}"
            f" + kits {_n(plan.kit_demand)}"
            + (f" + FBA prep {_n(plan.fba_prep_demand)}" if plan.fba_prep_demand else "")
            + (f" + safety {_n(plan.safety_stock)}" if plan.safety_stock else "")
        )
        add(
            f"      have: on hand {plan.on_hand}, on order {plan.on_order}, "
            f"in transit {plan.in_transit}   →  route: {plan.routing}"
        )
        for note in plan.notes:
            add(f"      note: {note}")
        add("")
    if not any(plan.order_units > 0 for plan in result.components):
        add("  Nothing needs ordering this week.")
        add("")

    add("NOTHING TO ORDER THIS WEEK")
    add(THIN)
    quiet = [plan for plan in result.components if plan.order_units <= 0]
    for plan in quiet:
        reason = (
            "not stocked, never purchased"
            if plan.component_class is ComponentClass.NON_STOCKED
            else f"covered — on hand {plan.on_hand}, on order {plan.on_order}"
        )
        add(f"  {plan.key}  {plan.name}: {reason}")
    add("")

    add("KITS — BUILD RECOMMENDATIONS")
    add(THIN)
    for kit in result.kits:
        if kit.demand_units == 0 and kit.build_recommendation == 0:
            continue
        add(f"  {kit.kit_group}  {kit.name}")
        add(
            f"      demand {kit.demand_units} over the cover period "
            f"({_n(kit.weekly_velocity)}/week), assembled stock {kit.assembled_stock}"
            f"  →  build {kit.build_recommendation}"
        )
        if kit.limiting_note:
            add(f"      can build {kit.buildable_now} right now — {kit.limiting_note}")
        if kit.unresolved_aliases:
            add("      channel SKUs still missing: " + ", ".join(kit.unresolved_aliases))
        if kit.allocation is not None:
            alloc = kit.allocation
            add(
                f"      allocation: warehouse {alloc.warehouse_on_hand}, keep "
                f"{alloc.mf_floor} for merchant orders, spare {alloc.allocatable}; "
                f"FBA target {alloc.fba_target} (has {alloc.fba_on_hand}, inbound "
                f"{alloc.fba_inbound}) → send {alloc.fba_send}"
            )
        add("")

    add("FBA INBOUND PLAN")
    add(THIN)
    if result.box_plan is None:
        add("  Nothing to send to Amazon this week.")
    else:
        boxes = result.box_plan
        add(f"  {boxes.boxes} boxes, packed identically:")
        for line in boxes.lines:
            add(
                f"    {line.sku}: {line.per_box} per box → {line.planned(boxes.boxes)} "
                f"(target {line.target}, short {line.shortfall(boxes.boxes)})"
            )
    add("")

    add("GAP LIST — order these by hand")
    add(THIN)
    if not result.gap_list:
        add("  Nothing on the gap list this week.")
    for entry in result.gap_list:
        add(
            f"  {entry.key}  {entry.name}: available {entry.available}, "
            f"suggested {entry.suggested_top_up}. {entry.reason}"
        )
    add("")

    add("BLOCKED — Shannon could not calculate these")
    add(THIN)
    if not context.blocked:
        add("  Nothing was blocked: every line in the parts list could be calculated.")
    for blocked in context.blocked:
        add(f"  {blocked}")
    add("")

    add("WARNINGS")
    add(THIN)
    warnings = list(result.warnings) + list(context.validation_warnings)
    if not warnings:
        add("  None.")
    for warning in warnings:
        add(f"  {warning}")
    add("")

    add("PARKING LOT — open questions, carried until you clear them")
    add(THIN)
    for item in config.boms.parking_lot:
        add(f"  {item.id}  {item.item}")
        if item.blocks:
            add(f"        blocks: {item.blocks}")
    add("")
    add(RULE)
    add(
        "Shannon, replenishment agent for "
        f"{context.entity_name}. Phase 1: she reads and calculates only."
    )
    add(RULE)
    return "\n".join(lines) + "\n"


__all__ = ["ReportContext", "render"]
