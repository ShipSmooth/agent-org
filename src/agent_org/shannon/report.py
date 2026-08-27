"""Shannon's report.

Written to a file and to the database, then emailed to Zach at iThrive.
The email is the only thing Shannon sends: no supplier hears from her, and
nothing is staged, ordered or replied to.

Every line shows the whole arithmetic — raw need, after the supplier's
minimum, after rounding to a multiple of five, in purchase units, and the
units that actually arrive — because the rounding cost should be visible,
not buried.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from agent_org.config.models import ComponentClass, LoadedConfig, ParkingLotItem
from agent_org.integrations.reads import OrderSignals
from agent_org.shannon.calculator import (
    ComponentPlan,
    ReplenishmentResult,
    Sufficiency,
    format_number,
    in_words,
)

RULE = "=" * 78
THIN = "-" * 78


_QUIET_SECTIONS: tuple[tuple[Sufficiency, str], ...] = (
    (
        Sufficiency.BLOCKING_BUILD,
        "OUT OF STOCK AND STOPPING A BUILD — nothing to order, but not fine:",
    ),
    (
        Sufficiency.ALREADY_PROPOSED,
        "Already proposed against the same hand count — not repeated:",
    ),
    (Sufficiency.COVERED, "Covered — stock meets the demand calculated for it:"),
    (Sufficiency.NO_DEMAND, "No demand this period — the arithmetic ran and produced zero:"),
    (Sufficiency.CANNOT_ASSESS, "Cannot be assessed — nothing to judge the stock against:"),
)


def _parking_lot_order(item: ParkingLotItem) -> tuple[int, str]:
    """PL-2 before PL-10: sort on the number, not on the text."""
    _, _, tail = item.id.partition("-")
    return (int(tail), item.id) if tail.isdigit() else (10**6, item.id)


# The calculator words some of its own sentences, so both sides print a
# number the same way: 245 rather than 245.0, 34.29 when it is not whole.
_n = format_number


def _heading(plan: ComponentPlan) -> str:
    """The line that names a component.

    Where the part number is ours rather than the supplier's, the product
    name leads and the reference is labelled as ours — otherwise it reads
    exactly like a SKU, and somebody quotes it to a supplier who has never
    heard of it.
    """
    if plan.part_is_internal_reference:
        return f"  {plan.supplier}  {plan.name}  (our reference {plan.key.part})"
    return f"  {plan.key}  {plan.name}"


def pack_overage_line(plan: ComponentPlan) -> str | None:
    """What a whole-pack purchase costs over what was needed, in words.

    A case of 240 against a need of 300 brings 480: the 180 spare is a
    decision Zach is making with his money, so it is said out loud.
    """
    if plan.actual_units is None or plan.actual_units <= plan.order_units:
        return None
    over = plan.actual_units - plan.order_units
    return (
        f"pack rounding: {plan.actual_units} arrive against a need of "
        f"{plan.order_units} — {over} more than needed, because this is only "
        f"sold in {plan.units_per_purchase_unit}s."
    )


def _route_name(routing: str) -> str:
    """`nar_cart` as Zach says it out loud, with the route kept alongside.

    The route strings are what the config and the coming cart-staging work
    key on, so they stay visible; nobody should have to translate between
    the summary and the section it summarises.
    """
    if routing == "gap_list":
        return "gap list — order by hand"
    if routing.endswith("_cart"):
        return f"{routing[: -len('_cart')].replace('_', ' ')} cart"
    return routing


def _count(number: int, singular: str, plural: str) -> str:
    """ "1 warning", not "1 warnings" — this is read at speed on a phone."""
    return f"{number} {singular if number == 1 else plural}"


@dataclass(frozen=True)
class ReportContext:
    entity_name: str
    generated_at: datetime
    config_changes: str
    validation_warnings: tuple[str, ...]
    order_signals: OrderSignals
    data_sources: tuple[str, ...]
    blocked: tuple[str, ...] = ()


def summary_block(
    result: ReplenishmentResult, config: LoadedConfig, context: ReportContext
) -> list[str]:
    """The first thing in the email: what needs doing, and how much of it.

    Everything here is counted from the same lists the sections below
    print, never from a second calculation — a summary that disagreed with
    its own report would be worse than no summary. The detail stays exactly
    where it was; this only says where to look first.
    """
    lines: list[str] = []
    add = lines.append

    to_order = [plan for plan in result.components if plan.order_units > 0]
    by_route: dict[str, int] = {}
    for plan in to_order:
        by_route[plan.routing] = by_route.get(plan.routing, 0) + 1
    blocking = [
        plan for plan in result.components if plan.sufficiency is Sufficiency.BLOCKING_BUILD
    ]
    warnings = len(result.warnings) + len(context.validation_warnings)
    open_parking = len([item for item in config.boms.parking_lot if not item.resolved]) + len(
        result.parking_lot_additions
    )

    add("AT A GLANCE")
    add(THIN)
    if to_order:
        add(f"  {_count(len(to_order), 'line', 'lines')} to order:")
        # Busiest route first: the question being answered is "what am I
        # doing first", and the routes are where the doing happens.
        for routing, count in sorted(by_route.items(), key=lambda pair: (-pair[1], pair[0])):
            add(f"      {routing:<22} {count:>3}   ({_route_name(routing)})")
    else:
        add("  Nothing to order.")
    if blocking:
        # Named, not counted: nothing else in the report is both urgent and
        # invisible in the order list, because the order quantity is zero.
        add(f"  {_count(len(blocking), 'line', 'lines')} out of stock and stopping a build:")
        for plan in blocking:
            add(f"      {plan.key}  {plan.name} — {plan.sufficiency_reason}.")
    else:
        add("  Nothing is out of stock and stopping a build.")
    add(f"  {_count(warnings, 'warning', 'warnings')}.")
    add(f"  {_count(open_parking, 'open parking-lot question', 'open parking-lot questions')}.")
    if context.blocked:
        add(
            f"  {_count(len(context.blocked), 'line', 'lines')} Shannon could not "
            "calculate at all — see BLOCKED."
        )
    add("")
    add("  The full report is below, unchanged. Nothing here is ordered or staged.")
    return lines


def render(result: ReplenishmentResult, config: LoadedConfig, context: ReportContext) -> str:
    lines: list[str] = []
    add = lines.append

    add(RULE)
    add(f"SHANNON — WEEKLY REPLENISHMENT REPORT — {context.entity_name}")
    add(f"Generated {context.generated_at:%A %d %B %Y, %H:%M %Z}")
    add(RULE)
    add("")
    lines.extend(summary_block(result, config, context))
    add("")
    add("PHASE 2 — READ ONLY, PLUS THIS EMAIL. Shannon read Veeqo and the inbox, did the")
    add("arithmetic, wrote this report, and emailed it to you. Nothing was ordered, no cart was")
    add("staged, no supplier heard from her, and no email was replied to or forwarded. Every")
    add("quantity below is a recommendation for you.")
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

    hand_counted = [plan for plan in result.components if plan.counted_by_hand]
    if hand_counted:
        add("COUNTED BY HAND — not in Veeqo, so Veeqo was not asked")
        add(THIN)
        for plan in hand_counted:
            add(f"  {plan.name} — {plan.hand_count_in_words()}")
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
        add(_heading(plan))
        if plan.part_is_internal_reference:
            # Orca publishes no item numbers, so the identifier is ours and
            # means nothing to them. Ordering quotes the product name.
            add(
                f"      order by product name: “{plan.name}”. {plan.key.part} is our "
                "own reference — this supplier has no part numbers, so never quote "
                "it on a purchase order."
            )
        add(
            f"      {_n(plan.raw_net)} → {plan.moq_rounded} → {plan.order_units} → "
            f"{purchase} → {actual}"
            + (f"   ({plan.purchase_unit_name})" if plan.purchase_unit_name else "")
        )
        overage = pack_overage_line(plan)
        if overage is not None:
            add(f"      {overage}")
        if plan.component_class is ComponentClass.REORDER_POINT:
            # Nothing is forecast for these. Two different instructions live
            # here and they are printed differently: buy a fixed quantity, or
            # top up to a level.
            if plan.reorder_quantity is not None:
                add(
                    f"      below the reorder point of {plan.reorder_point} — buy "
                    f"{in_words(plan.reorder_quantity)} (standing order quantity)"
                    + (f" + FBA prep {_n(plan.fba_prep_demand)}" if plan.fba_prep_demand else "")
                )
            else:
                add(
                    f"      top up to {_n(plan.gross_demand)} (below the reorder point "
                    f"of {plan.reorder_point})"
                    + (f" + FBA prep {_n(plan.fba_prep_demand)}" if plan.fba_prep_demand else "")
                )
        else:
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
        if plan.sales_asins:
            # Recognition only. Sales were read against the channel SKUs; an
            # ASIN can be shared by three colourways and cannot say which sold.
            add("      listed on Amazon under: " + ", ".join(plan.sales_asins))
        for note in plan.notes:
            add(f"      note: {note}")
        add("")
    if not any(plan.order_units > 0 for plan in result.components):
        add("  Nothing needs ordering this week.")
        add("")

    add("NOTHING TO ORDER THIS WEEK")
    add(THIN)
    quiet = [
        plan
        for plan in result.components
        if plan.order_units <= 0 and plan.component_class is not ComponentClass.NON_STOCKED
    ]
    # Split by WHY a line needs nothing. One word for all four states was
    # how a part with nothing on hand, and a build waiting on it, came to be
    # printed as "covered" three lines above being called urgent.
    for state, heading in _QUIET_SECTIONS:
        group = [plan for plan in quiet if plan.sufficiency is state]
        if not group:
            continue
        add(f"  {heading}")
        for plan in group:
            add(f"{_heading(plan)}")
            add(f"      {plan.sufficiency_reason}.")
            # All three, because the figure above is their sum: two of them
            # printed under a total made of three is how a reader checks the
            # arithmetic and finds it apparently wrong.
            add(
                f"      on hand {plan.on_hand}, on order {plan.on_order}, "
                f"in transit {plan.in_transit}"
            )
        add("")
    if not quiet:
        add("  Every line in the parts list needs something ordered.")
        add("")

    # Stated, never omitted: a missing line and a zero line read the same on
    # paper, and only one of them proves the class did its job.
    add("NOT STOCKED — QUANTITY 0, ALWAYS")
    add(THIN)
    non_stocked = [
        plan for plan in result.components if plan.component_class is ComponentClass.NON_STOCKED
    ]
    if not non_stocked:
        add("  No non-stocked components in this parts list.")
    for plan in non_stocked:
        add(f"  {plan.key}  {plan.name}")
        add(
            f"      order 0 (class non_stocked). Demand of {_n(plan.gross_demand)} from the "
            "kits is listed for costing only; Shannon never buys this line and never "
            "lets it block a build."
        )
    add("")

    add("KITS — BUILD RECOMMENDATIONS")
    add(THIN)
    for kit in result.kits:
        if kit.demand_units == 0 and kit.build_recommendation == 0:
            continue
        add(f"  {kit.family}  {kit.name}")
        add(
            f"      demand {kit.demand_units} over the cover period "
            f"({_n(kit.weekly_velocity)}/week), assembled stock {kit.assembled_stock}"
            f"  →  build {kit.build_recommendation}"
        )
        if len(kit.members) > 1:
            add(f"      split of those {kit.build_recommendation}:")
        for member in kit.members:
            detail = (
                f"      {member.kit_group}: build {member.build_share} "
                f"(demand {member.demand_units}, stock {member.assembled_stock}); "
                f"can build {member.buildable_now} from stock on hand"
            )
            add(detail)
            if member.limiting_note:
                add(f"        limited by {member.limiting_note}")
            if member.build_blocked:
                add(f"        {member.build_blocked}")
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
            if alloc.fba_send > 0 and len(kit.members) > 1:
                # The send is one shipment for the family, divided by how
                # fast each colourway sells. Zach packs the boxes, so he
                # needs the split, not just the total.
                split = ", ".join(
                    f"{member.kit_group} {member.fba_send_share}" for member in kit.members
                )
                add(f"      split of that send, in proportion to demand: {split}")
        add("")

    # An inactive listing is not zero demand: Zach takes a listing down when he
    # is out of stock, so its sales measure the listing rather than the market.
    add("DEMAND SUPPRESSED — every Amazon listing is inactive, so the sales are not the demand")
    add(THIN)
    if not result.suppressed:
        add("  None: every listed kit and component is live on at least one channel.")
    for dead in result.suppressed:
        add(f"  {dead.subject}  {dead.name} ({dead.kind})")
        add("      inactive listings: " + ", ".join(dead.channels))
        if dead.current_weekly > 0:
            add(
                f"      still selling {_n(dead.current_weekly)}/week away from Amazon. "
                "Treat that as the floor, not the demand: nobody can buy it on "
                "Amazon at all."
            )
        else:
            add(
                "      recent sales nil — which measures the listing, not the market, "
                "so Shannon has not forecast it."
            )
        if dead.historical_weekly is not None:
            add(
                f"      historical, before it came down: {_n(dead.historical_weekly)}"
                f"/week over {dead.historical_window_days} days."
            )
        else:
            add(
                "      no sales history reaches back before the listing came down, so "
                "there is no honest figure to give you. Zero is not it."
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
    live = sorted(
        (item for item in config.boms.parking_lot if not item.resolved), key=_parking_lot_order
    )
    closed = sorted(
        (item for item in config.boms.parking_lot if item.resolved), key=_parking_lot_order
    )
    for item in live:
        add(f"  {item.id}  {item.item}")
        if item.blocks:
            add(f"        blocks: {item.blocks}")
    # Added by this run rather than by the config file: only Zach can decide
    # whether a suppressed line is restocked and relisted, or discontinued.
    for addition in result.parking_lot_additions:
        add(f"  {addition.id}  {addition.item}")
        add(f"        {addition.detail}")
        add(f"        blocks: {addition.blocks}")
    if not live and not result.parking_lot_additions:
        add("  Nothing open.")
    if closed:
        add("")
        add("  Closed — settled, kept for the record, nothing for you to do:")
        for item in closed:
            add(f"    {item.id}  {item.item}")
    add("")
    add(RULE)
    add(
        "Shannon, replenishment agent for "
        f"{context.entity_name}. Phase 2: she reads, calculates, and emails this to you."
    )
    add(RULE)
    return "\n".join(lines) + "\n"


__all__ = ["ReportContext", "render", "summary_block"]
