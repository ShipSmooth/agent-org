"""The replenishment calculator — docs/replenishment.md, exactly.

Pure functions: fixtures in, numbers out, no I/O and no model calls.
Every quantity traces to the documented arithmetic:

- demand runs in sellable units; ordering in purchase units (§6.1, last);
- H = cover_target_weeks, INCLUSIVE of lead time (§3);
- rounding order: MOQ round-up, then nearest 5, then pack conversion (§6);
- BOM explosion across ALL kits, standalone plus kit demand summed (§2/§3);
- channel allocation (§7), build recommendations with the limiting
  component named (§10 step 7), FBA box planning (§8).

Exact arithmetic uses Fraction; nothing is floated until display.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction

from agent_org.integrations.gmail import OnOrderResult
from agent_org.integrations.veeqo import VeeqoSnapshot
from agent_org.shannon.config_model import ComponentKey, EntityConfig, KitCfg

_NAR_SKU_RE = re.compile(r"^\d{2}-\d{4}$")


class RunStopped(RuntimeError):
    """The run must stop and say so — never guess (docs/replenishment.md §2.1)."""


def moq_round(q: Fraction | int, moq_min: int, moq_increment: int) -> int:
    """Always round UP (§6). q ≤ 0 → 0; q ≤ min → min; else min + ceil steps."""
    if q <= 0:
        return 0
    if q <= moq_min:
        return moq_min
    return moq_min + math.ceil(Fraction(q - moq_min, moq_increment)) * moq_increment


def round_up_to(q: int, nearest: int) -> int:
    if q <= 0:
        return 0
    return math.ceil(Fraction(q, nearest)) * nearest


@dataclass
class OrderLine:
    """A forecast-class purchase line, every rounding stage in the open."""

    key: ComponentKey
    name: str
    standalone_demand: Fraction
    kit_demand: Fraction
    fba_prep_demand: int
    safety_stock: Fraction
    gross_demand: Fraction
    on_hand: int
    on_order: int
    in_transit: int
    net_requirement: Fraction  # raw, before any rounding; may be negative
    moq_rounded: int
    order_units: int  # after nearest-5; sellable units
    units_per_purchase_unit: int
    purchase_units: int  # what would go in a cart — NEVER order_units
    actual_units: int
    flags: list[str] = field(default_factory=list)


@dataclass
class TopUpLine:
    """A reorder_point-class line: threshold logic, no forecast."""

    key: ComponentKey
    name: str
    available: int
    reorder_point: int
    reorder_target: int
    top_up: int  # rounded per §6 (nearest 5)
    routing: str  # 'dynarex_cart' | 'amazon_cart' | 'gap_list' | 'prompt'
    flags: list[str] = field(default_factory=list)


@dataclass
class GapItem:
    key: ComponentKey
    name: str
    supplier: str
    reason: str
    available: int | None = None
    threshold: int | None = None
    suggested_top_up: int | None = None


@dataclass
class BuildRec:
    kit_group: str
    name: str
    demand: int
    assembled: int
    build: int
    feasible_units: int | None  # None = unlimited (nothing constrains it)
    limiting_component: str | None
    blocked_note: str | None


@dataclass
class Allocation:
    sku: str
    warehouse_on_hand: int
    mf_floor: int
    allocatable: int
    fba_target: int
    fba_on_hand: int
    fba_inbound: int
    fba_send: int
    walmart_reserve: int


@dataclass
class BoxPlanLine:
    sku: str
    target: int
    per_box: int
    planned: int
    delta: int  # planned - target (negative = shortfall)


@dataclass
class BoxPlan:
    boxes: int
    lines: list[BoxPlanLine]
    total_error: int


@dataclass(frozen=True)
class NonStockedLine:
    """A non_stocked component, stated at quantity 0 rather than omitted."""

    key: ComponentKey
    name: str
    purchase_units: int
    note: str


@dataclass
class RunResult:
    order_lines: list[OrderLine]
    top_ups: list[TopUpLine]
    gap_list: list[GapItem]
    builds: list[BuildRec]
    allocations: list[Allocation]
    box_plan: BoxPlan | None
    non_stocked: list[NonStockedLine]
    prep_need: dict[ComponentKey, int]
    flags: list[str]


def _merchant_channels(cfg: EntityConfig) -> set[str]:
    return {c.key for c in cfg.channels if c.fulfillment == "merchant"}


def kit_weekly_velocity(cfg: EntityConfig, kit: KitCfg, snap: VeeqoSnapshot) -> dict[str, Fraction]:
    """Per channel, summing sales across all of the kit's aliases (§2.2)."""
    out: dict[str, Fraction] = {}
    for ch, sku in kit.aliases.items():
        if sku.strip().upper() == "TODO":
            continue
        out[ch] = out.get(ch, Fraction(0)) + snap.weekly_velocity(sku, ch)
    return out


def classify_sold_skus(cfg: EntityConfig, snap: VeeqoSnapshot) -> None:
    """A kit selling anywhere with no BOM entry is a HARD run failure (§2.1)."""
    known: set[str] = set()
    for kit in cfg.kits.values():
        known.update(s for s in kit.aliases.values() if s.strip().upper() != "TODO")
    known.update(sp.sku for sp in cfg.standalone_products)
    known.update(c.key.part for c in cfg.components.values())
    unknown = [
        sku for sku in sorted(snap.sold_skus()) if sku not in known and not _NAR_SKU_RE.match(sku)
    ]
    if unknown:
        raise RunStopped(
            f"These SKUs sold in the window but match no kit alias, no standalone "
            f"product and no component: {', '.join(unknown)}. Silently un-exploded "
            "demand is exactly the bug this check exists to prevent, so the run "
            "stops here."
        )


def _standalone_sku_for(cfg: EntityConfig, key: ComponentKey) -> str | None:
    for sp in cfg.standalone_products:
        if sp.key == key:
            return sp.sku
    return None


def allocate(
    *,
    warehouse_on_hand: int,
    fba_on_hand: int,
    fba_inbound: int,
    mf_weekly_velocity: Fraction,
    fba_weekly_velocity: Fraction,
    mf_floor_weeks: Fraction,
    fba_cover_weeks: Fraction,
    walmart_reserve_units: int,
    sku: str,
) -> Allocation:
    """§7 — merchant-fulfilled floor first, then FBA, then Walmart reserve."""
    mf_floor = math.ceil(mf_floor_weeks * mf_weekly_velocity)
    allocatable = max(warehouse_on_hand - mf_floor, 0)
    fba_target = math.ceil(fba_cover_weeks * fba_weekly_velocity)
    want = fba_target - fba_on_hand - fba_inbound
    fba_send = min(max(want, 0), allocatable)
    return Allocation(
        sku=sku,
        warehouse_on_hand=warehouse_on_hand,
        mf_floor=mf_floor,
        allocatable=allocatable,
        fba_target=fba_target,
        fba_on_hand=fba_on_hand,
        fba_inbound=fba_inbound,
        fba_send=fba_send,
        walmart_reserve=walmart_reserve_units,
    )


def plan_boxes(
    targets: dict[str, int], *, box_min: int, box_max: int, overship_tolerance: int
) -> BoxPlan | None:
    """§8 — brute force over B and per-SKU Q; ties break toward fewer boxes."""
    live = {sku: t for sku, t in targets.items() if t > 0}
    if not live:
        return None
    best: BoxPlan | None = None
    for boxes in range(box_min, box_max + 1):
        lines: list[BoxPlanLine] = []
        error = 0
        any_nonzero = False
        for sku, target in sorted(live.items()):
            candidates = {target // boxes, round(target / boxes)}
            best_q = 0
            best_err: int | None = None
            for q in candidates:
                if q < 0 or boxes * q > target + overship_tolerance:
                    continue
                err = abs(boxes * q - target)
                if best_err is None or err < best_err:
                    best_err, best_q = err, q
            planned = boxes * best_q
            if best_q > 0:
                any_nonzero = True
            lines.append(BoxPlanLine(sku, target, best_q, planned, planned - target))
            error += abs(planned - target)
        if not any_nonzero:
            continue
        if best is None or error < best.total_error:
            best = BoxPlan(boxes=boxes, lines=lines, total_error=error)
    return best


def run_calculation(cfg: EntityConfig, snap: VeeqoSnapshot, on_order: OnOrderResult) -> RunResult:
    params = cfg.shannon.params
    h = params.cover_target_weeks
    merchant = _merchant_channels(cfg)
    flags: list[str] = list(on_order.split_shipment_flags)

    classify_sold_skus(cfg, snap)

    # ---- kit velocities and forecasts (§2.2) ----
    kit_vel: dict[str, dict[str, Fraction]] = {
        kg: kit_weekly_velocity(cfg, kit, snap) for kg, kit in cfg.kits.items()
    }
    kit_forecast: dict[str, Fraction] = {
        kg: sum(per_ch.values(), Fraction(0)) * h for kg in cfg.kits for per_ch in [kit_vel[kg]]
    }

    # ---- kit assembled stock, builds and allocation (§7, §10 step 7) ----
    builds: list[BuildRec] = []
    allocations: list[Allocation] = []
    fba_send_by_kit: dict[str, int] = {}
    for kg, kit in cfg.kits.items():
        warehouse = 0
        fba = 0
        inbound = 0
        for sku in {s for s in kit.aliases.values() if s.strip().upper() != "TODO"}:
            level = snap.stock.get(sku)
            if level is not None:
                warehouse += level.warehouse_available
                fba += level.fba_sellable
            inbound += snap.fba_inbound.get(sku, 0)
        demand = math.ceil(kit_forecast[kg])
        assembled = warehouse + fba
        build = max(demand - assembled, 0)
        feasible, limiting = _build_feasibility(cfg, kit, snap)
        builds.append(
            BuildRec(
                kit_group=kg,
                name=kit.name,
                demand=demand,
                assembled=assembled,
                build=build,
                feasible_units=feasible,
                limiting_component=limiting,
                blocked_note=kit.build_blocked,
            )
        )
        per_ch = kit_vel[kg]
        alloc = allocate(
            warehouse_on_hand=warehouse,
            fba_on_hand=fba,
            fba_inbound=inbound,
            mf_weekly_velocity=sum((v for ch, v in per_ch.items() if ch in merchant), Fraction(0)),
            fba_weekly_velocity=per_ch.get("fba", Fraction(0)),
            mf_floor_weeks=params.mf_floor_weeks,
            fba_cover_weeks=params.fba_cover_weeks,
            walmart_reserve_units=params.walmart_reserve_units,
            sku=kg,
        )
        allocations.append(alloc)
        fba_send_by_kit[kg] = alloc.fba_send

    # ---- standalone product allocation ----
    fba_send_by_sku: dict[str, int] = {}
    for sp in cfg.standalone_products:
        level = snap.stock.get(sp.sku)
        if level is None:
            continue
        per_ch = {
            ch: snap.weekly_velocity(sp.sku, ch) for ch in snap.velocity_units.get(sp.sku, {})
        }
        alloc = allocate(
            warehouse_on_hand=level.warehouse_available,
            fba_on_hand=level.fba_sellable,
            fba_inbound=snap.fba_inbound.get(sp.sku, 0),
            mf_weekly_velocity=sum((v for ch, v in per_ch.items() if ch in merchant), Fraction(0)),
            fba_weekly_velocity=per_ch.get("fba", Fraction(0)),
            mf_floor_weeks=params.mf_floor_weeks,
            fba_cover_weeks=params.fba_cover_weeks,
            walmart_reserve_units=params.walmart_reserve_units,
            sku=sp.sku,
        )
        allocations.append(alloc)
        fba_send_by_sku[sp.sku] = alloc.fba_send

    total_fba_units = sum(fba_send_by_kit.values()) + sum(fba_send_by_sku.values())

    # ---- FBA-prep consumption (channels: [fba] lines and §2.1.1 block) ----
    prep_need: dict[ComponentKey, int] = {}
    for kg, kit in cfg.kits.items():
        for line in kit.components:
            if line.channels is not None and "fba" in line.channels:
                prep_need[line.key] = prep_need.get(line.key, 0) + line.qty * fba_send_by_kit.get(
                    kg, 0
                )
    for prep in cfg.standalone_prep:
        if prep.applies_to_all:
            prep_need[prep.key] = prep_need.get(prep.key, 0) + prep.qty * total_fba_units
        elif prep.sku is not None and prep.sku in fba_send_by_sku:
            prep_need[prep.key] = prep_need.get(prep.key, 0) + prep.qty * fba_send_by_sku[prep.sku]

    # ---- component demand explosion (§2.2, §3) ----
    order_lines: list[OrderLine] = []
    top_ups: list[TopUpLine] = []
    gap_list: list[GapItem] = []
    non_stocked: list[NonStockedLine] = []

    for key, comp in cfg.components.items():
        if comp.cls == "ops_consumable":
            continue  # reminder path only; never counted, never forecast (§4.1)
        if comp.cls == "non_stocked":
            # purchase quantity is always 0, and it is stated, not omitted:
            # a silent absence looks like an oversight (§10 step 6).
            non_stocked.append(
                NonStockedLine(
                    key=key,
                    name=comp.name,
                    purchase_units=0,
                    note=(
                        "class non_stocked — carried in the BOM for description and "
                        "cost only. Never purchased, never counted, treated as "
                        "unlimited when checking whether a kit can be built."
                    ),
                )
            )
            continue

        level = snap.stock.get(key.part)
        on_hand = level.on_hand if level else 0
        on_order_units = on_order.units_on_order(key.part)
        in_transit = snap.fba_inbound.get(key.part, 0) if level else 0

        if comp.cls == "forecast":
            standalone_sku = _standalone_sku_for(cfg, key)
            standalone_vel = (
                snap.total_weekly_velocity(standalone_sku) if standalone_sku else Fraction(0)
            )
            # demand is read via the sales side only; a purchase_asin never
            # creates demand (§5).
            standalone_demand = standalone_vel * h
            kit_vel_sum = Fraction(0)
            for kg, kit in cfg.kits.items():
                for line in [*kit.components, *([kit.pouch] if kit.pouch else [])]:
                    if line.key == key and line.channels is None:
                        kit_vel_sum += line.qty * sum(kit_vel[kg].values(), Fraction(0))
            kit_demand = kit_vel_sum * h
            safety = params.safety_stock_weeks * (standalone_vel + kit_vel_sum)
            fba_prep = prep_need.get(key, 0)
            gross = standalone_demand + kit_demand + safety + fba_prep
            net = gross - on_hand - on_order_units - in_transit
            moq_rounded = moq_round(net, comp.moq_min, comp.moq_increment)
            order_units = round_up_to(moq_rounded, params.round_up_to_nearest)
            upu = comp.units_per_purchase_unit or 1
            line_flags: list[str] = []
            if comp.units_per_purchase_unit is None:
                line_flags.append(
                    "pack size not yet confirmed (discovery mode) — purchase units "
                    "assume 1 until Zach confirms"
                )
            if level is None and gross > 0:
                line_flags.append("no Veeqo stock record — on_hand treated as 0, check this")
            if gross == 0 and on_hand <= 0:
                line_flags.append("zero sales in the window — possibly a mis-mapped listing")
            purchase_units = math.ceil(Fraction(order_units, upu))
            order_lines.append(
                OrderLine(
                    key=key,
                    name=comp.name,
                    standalone_demand=standalone_demand,
                    kit_demand=kit_demand,
                    fba_prep_demand=fba_prep,
                    safety_stock=safety,
                    gross_demand=gross,
                    on_hand=on_hand,
                    on_order=on_order_units,
                    in_transit=in_transit,
                    net_requirement=net,
                    moq_rounded=moq_rounded,
                    order_units=order_units,
                    units_per_purchase_unit=upu,
                    purchase_units=purchase_units,
                    actual_units=purchase_units * upu,
                    flags=line_flags,
                )
            )
        elif comp.cls == "reorder_point":
            available = on_hand + on_order_units + in_transit
            if comp.reorder_point is None or comp.reorder_target is None:
                gap_list.append(
                    GapItem(
                        key=key,
                        name=comp.name,
                        supplier=key.supplier,
                        reason=(
                            "reorder thresholds are still TODO — Zach needs to set "
                            "reorder_point and reorder_target before this line can be "
                            "computed"
                        ),
                        available=available if level else None,
                    )
                )
                continue
            if level is None:
                gap_list.append(
                    GapItem(
                        key=key,
                        name=comp.name,
                        supplier=key.supplier,
                        reason="no stock data in Veeqo — cannot compare against the threshold",
                        threshold=comp.reorder_point,
                    )
                )
                continue
            if available < comp.reorder_point:
                top_up = round_up_to(comp.reorder_target - available, params.round_up_to_nearest)
                routing, routed_flags = _route(cfg, key)
                top_ups.append(
                    TopUpLine(
                        key=key,
                        name=comp.name,
                        available=available,
                        reorder_point=comp.reorder_point,
                        reorder_target=comp.reorder_target,
                        top_up=top_up,
                        routing=routing,
                        flags=routed_flags,
                    )
                )
                if routing in ("gap_list", "prompt"):
                    gap_list.append(
                        GapItem(
                            key=key,
                            name=comp.name,
                            supplier=key.supplier,
                            reason=(
                                "stock is below the threshold and this supplier has no "
                                "ordering path — Zach (or Justin) orders by hand"
                                if routing == "gap_list"
                                else "stock is low; Shannon prompts and never picks a "
                                "supplier on Zach's behalf"
                            ),
                            available=available,
                            threshold=comp.reorder_point,
                            suggested_top_up=top_up,
                        )
                    )

    order_lines.sort(key=lambda line: (line.key.supplier, line.key.part))
    top_ups.sort(key=lambda line: (line.key.supplier, line.key.part))

    box_plan = plan_boxes(
        {**{kg: send for kg, send in fba_send_by_kit.items()}, **fba_send_by_sku},
        box_min=params.box_min,
        box_max=params.box_max,
        overship_tolerance=params.overship_tolerance,
    )

    return RunResult(
        order_lines=order_lines,
        top_ups=top_ups,
        gap_list=gap_list,
        builds=builds,
        allocations=allocations,
        box_plan=box_plan,
        non_stocked=non_stocked,
        prep_need=prep_need,
        flags=flags,
    )


def _route(cfg: EntityConfig, key: ComponentKey) -> tuple[str, list[str]]:
    supplier = cfg.suppliers.get(key.supplier)
    if supplier is None:
        return "gap_list", ["supplier unresolved — flagged"]
    if key.supplier in ("internal", "unsourced"):
        return "prompt", []
    if supplier.acquisition == "browser" and key.supplier == "dynarex":
        return "dynarex_cart", [
            "would be a staged dynarex.com cart in a later phase; report-only in Phase 1"
        ]
    if supplier.acquisition == "cart_url":
        return "amazon_cart", [
            "would be an offered Amazon Business cart in a later phase; report-only in Phase 1"
        ]
    return "gap_list", []


def _build_feasibility(
    cfg: EntityConfig, kit: KitCfg, snap: VeeqoSnapshot
) -> tuple[int | None, str | None]:
    """Max buildable units and the limiting component (§10 step 7).

    non_stocked components count as infinite supply; channels: [fba] prep
    lines are consumed at prep time, not at assembly, so they do not
    constrain the build.
    """
    feasible: int | None = None
    limiting: str | None = None
    lines = [*kit.components, *([kit.pouch] if kit.pouch else [])]
    for line in lines:
        comp = cfg.components.get(line.key)
        if comp is None or comp.cls in ("non_stocked", "ops_consumable"):
            continue
        if line.channels is not None:
            continue
        level = snap.stock.get(line.key.part)
        available = level.on_hand if level else 0
        can_build = max(available, 0) // line.qty
        if feasible is None or can_build < feasible:
            feasible = can_build
            limiting = f"{comp.name} ({line.key})"
    return feasible, limiting
