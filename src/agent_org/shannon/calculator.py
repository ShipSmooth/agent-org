"""The replenishment calculation (docs/replenishment.md §2–§8).

Pure arithmetic, no model, no input/output. Everything it needs is handed
to it; everything it produces is returned. That is what makes the numbers
checkable by hand — which is the point of the whole phase.

Two conventions the specification leaves open, chosen here and stated so
the arithmetic can be reproduced:

* Demand is held as exact fractions all the way through and rounded up to
  a whole unit once, at the net requirement, before MOQ rounding. Rounding
  up is the same instinct as the MOQ rule: never be short on a
  safety-critical item. (Every number in the worked example is exact, so
  this convention changes nothing there.)
* In the channel allocation the merchant-fulfilled floor rounds **up** and
  the FBA target rounds **down**. Both favour the warehouse, which is the
  documented priority order: Justin has to be able to ship tomorrow's
  merchant orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction

from agent_org.config.models import (
    Capability,
    Component,
    ComponentClass,
    ComponentKey,
    LoadedConfig,
    Parameters,
)
from agent_org.integrations.reads import InboundShipment, SalesVelocity, StockPosition
from agent_org.shannon.boxes import BoxPlan, plan_boxes

GAP_LIST = "gap_list"
NO_PURCHASE = "none"
CART_SUFFIX = "_cart"


def ceil_fraction(value: Fraction) -> int:
    return math.ceil(value)


def moq_round(quantity: int, component: Component) -> int:
    """Round up to the supplier's minimum and increment. Always up."""
    if quantity <= 0:
        return 0
    if quantity <= component.moq_min:
        return component.moq_min
    increment = max(component.moq_increment, 1)
    over = quantity - component.moq_min
    return component.moq_min + math.ceil(over / increment) * increment


def round_up_to(quantity: int, step: int) -> int:
    if quantity <= 0 or step <= 1:
        return max(quantity, 0)
    return math.ceil(quantity / step) * step


@dataclass(frozen=True)
class Allocation:
    """Where one sellable SKU's stock should go (docs/replenishment.md §7)."""

    sku: str
    warehouse_on_hand: int
    fba_on_hand: int
    fba_inbound: int
    mf_floor: int
    allocatable: int
    fba_target: int
    wanted_at_fba: int
    fba_send: int
    walmart_reserve: int


@dataclass(frozen=True)
class KitPlan:
    kit_group: str
    name: str
    weekly_velocity: Fraction
    demand_units: int
    assembled_stock: int
    build_recommendation: int
    buildable_now: int
    limiting_component: ComponentKey | None
    limiting_note: str | None
    allocation: Allocation | None
    unresolved_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComponentPlan:
    """One line of the report, with every intermediate number kept."""

    key: ComponentKey
    name: str
    component_class: ComponentClass
    supplier: str
    standalone_units_sold: int
    standalone_weekly: Fraction
    standalone_demand: Fraction
    kit_demand: Fraction
    fba_prep_demand: Fraction
    safety_stock: Fraction
    gross_demand: Fraction
    on_hand: int
    on_order: int
    in_transit: int
    raw_net: Fraction
    net_units: int
    moq_rounded: int
    order_units: int
    units_per_purchase_unit: int | None
    purchase_units: int | None
    actual_units: int | None
    purchase_unit_name: str | None
    routing: str
    reorder_point: int | None = None
    reorder_target: int | None = None
    available: int | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GapListEntry:
    key: ComponentKey
    name: str
    supplier: str
    available: int | None
    threshold: int | None
    suggested_top_up: int
    reason: str


@dataclass
class ReplenishmentResult:
    bom_version: str
    parameters: Parameters
    kits: tuple[KitPlan, ...]
    components: tuple[ComponentPlan, ...]
    gap_list: tuple[GapListEntry, ...]
    box_plan: BoxPlan | None
    fba_send_targets: dict[str, int]
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def component(self, supplier: str, part: str) -> ComponentPlan:
        key = ComponentKey(supplier=supplier, part=part)
        for plan in self.components:
            if plan.key == key:
                return plan
        raise KeyError(f"No line for {key} in this run.")

    def kit(self, kit_group: str) -> KitPlan:
        for plan in self.kits:
            if plan.kit_group == kit_group:
                return plan
        raise KeyError(f"No kit plan for {kit_group} in this run.")


class ReplenishmentCalculator:
    def __init__(
        self,
        config: LoadedConfig,
        stock: dict[str, StockPosition],
        velocity: dict[str, SalesVelocity],
        inbound: dict[str, InboundShipment],
        on_order: dict[str, int],
    ) -> None:
        self.config = config
        self.stock = stock
        self.velocity = velocity
        self.inbound = inbound
        self.on_order = on_order
        self.params = config.shannon.parameters
        self.warnings: list[str] = []
        self._fba_channels = tuple(
            channel.key for channel in config.entity.channels if channel.fulfillment == "fba"
        )
        self._merchant_channels = tuple(
            channel.key for channel in config.entity.channels if channel.fulfillment != "fba"
        )

    # ---------------------------------------------------------------- helpers

    def _stock_of(self, sku: str) -> StockPosition:
        return self.stock.get(sku, StockPosition(sku=sku, warehouse_available=0, fba_sellable=0))

    def _velocity_of(self, sku: str) -> SalesVelocity:
        return self.velocity.get(
            sku,
            SalesVelocity(sku=sku, units_sold=0, window_days=self.params.velocity_window_days),
        )

    def _inbound_of(self, sku: str) -> int:
        shipment = self.inbound.get(sku)
        return shipment.units if shipment else 0

    def _cover(self, component: Component | None) -> Fraction:
        if component is not None and component.cover_target_weeks is not None:
            return component.cover_target_weeks
        return self.params.cover_target_weeks

    def _allocate(self, sku: str, weekly_by_channel: dict[str, Fraction]) -> Allocation:
        position = self._stock_of(sku)
        merchant_weekly = sum(
            (weekly_by_channel.get(channel, Fraction(0)) for channel in self._merchant_channels),
            Fraction(0),
        )
        fba_weekly = sum(
            (weekly_by_channel.get(channel, Fraction(0)) for channel in self._fba_channels),
            Fraction(0),
        )
        mf_floor = math.ceil(self.params.mf_floor_weeks * merchant_weekly)
        allocatable = max(position.warehouse_available - mf_floor, 0)
        fba_target = math.floor(self.params.fba_cover_weeks * fba_weekly)
        inbound = self._inbound_of(sku)
        wanted = fba_target - position.fba_sellable - inbound
        fba_send = max(min(wanted, allocatable), 0)
        return Allocation(
            sku=sku,
            warehouse_on_hand=position.warehouse_available,
            fba_on_hand=position.fba_sellable,
            fba_inbound=inbound,
            mf_floor=mf_floor,
            allocatable=allocatable,
            fba_target=fba_target,
            wanted_at_fba=wanted,
            fba_send=fba_send,
            walmart_reserve=self.params.walmart_reserve_units,
        )

    # ------------------------------------------------------------------- kits

    def _kit_channel_velocity(self, kit_group: str) -> dict[str, Fraction]:
        kit = self.config.boms.kits[kit_group]
        weekly: dict[str, Fraction] = {}
        for channel_key, sku in kit.aliases.items():
            if sku is None:
                continue
            sales = self._velocity_of(sku)
            if sales.by_channel:
                for channel, units in sales.by_channel.items():
                    weekly[channel] = weekly.get(channel, Fraction(0)) + Fraction(
                        units * 7, sales.window_days
                    )
            else:
                weekly[channel_key] = weekly.get(channel_key, Fraction(0)) + sales.weekly()
        return weekly

    def _kit_assembled_stock(self, kit_group: str) -> tuple[int, int, int]:
        kit = self.config.boms.kits[kit_group]
        warehouse = 0
        fba = 0
        for sku in kit.aliases.values():
            if sku is None:
                continue
            position = self._stock_of(sku)
            warehouse += position.warehouse_available
            fba += position.fba_sellable
        return warehouse, fba, warehouse + fba

    def _kit_plans(self) -> tuple[list[KitPlan], dict[str, Fraction]]:
        """Kit demand, build recommendations and allocation, in that order."""
        plans: list[KitPlan] = []
        kit_demand: dict[str, Fraction] = {}
        horizon = self.params.cover_target_weeks

        for kit_group, kit in sorted(self.config.boms.kits.items()):
            weekly_by_channel = self._kit_channel_velocity(kit_group)
            weekly = sum(weekly_by_channel.values(), Fraction(0))
            demand = weekly * horizon
            kit_demand[kit_group] = demand
            demand_units = ceil_fraction(demand)
            warehouse, _fba, assembled = self._kit_assembled_stock(kit_group)
            build = max(demand_units - assembled, 0)
            buildable, limiting, limiting_note = self._build_feasibility(kit_group)
            unresolved = tuple(
                channel for channel, sku in sorted(kit.aliases.items()) if sku is None
            )

            allocation: Allocation | None = None
            resolved_fba_alias = next(
                (
                    sku
                    for channel, sku in sorted(kit.aliases.items())
                    if sku is not None and channel in self._fba_channels
                ),
                None,
            )
            if resolved_fba_alias is not None:
                allocation = self._allocate_kit(kit_group, weekly_by_channel, warehouse)

            if build > buildable:
                self.warnings.append(
                    f"{kit.name}: {build} to build but only {buildable} can be "
                    f"assembled from stock on hand"
                    + (f" — {limiting_note}" if limiting_note else "")
                    + "."
                )

            plans.append(
                KitPlan(
                    kit_group=kit_group,
                    name=kit.name,
                    weekly_velocity=weekly,
                    demand_units=demand_units,
                    assembled_stock=assembled,
                    build_recommendation=build,
                    buildable_now=buildable,
                    limiting_component=limiting,
                    limiting_note=limiting_note,
                    allocation=allocation,
                    unresolved_aliases=unresolved,
                )
            )
        return plans, kit_demand

    def _allocate_kit(
        self, kit_group: str, weekly_by_channel: dict[str, Fraction], warehouse: int
    ) -> Allocation:
        """Allocation for a kit spans its channel aliases, so the stock figures
        are summed across them and the allocation is reported under the kit."""
        kit = self.config.boms.kits[kit_group]
        fba_on_hand = 0
        inbound = 0
        for channel, sku in kit.aliases.items():
            if sku is None:
                continue
            if channel in self._fba_channels:
                fba_on_hand += self._stock_of(sku).fba_sellable
                inbound += self._inbound_of(sku)
            else:
                fba_on_hand += 0
        merchant_weekly = sum(
            (weekly_by_channel.get(channel, Fraction(0)) for channel in self._merchant_channels),
            Fraction(0),
        )
        fba_weekly = sum(
            (weekly_by_channel.get(channel, Fraction(0)) for channel in self._fba_channels),
            Fraction(0),
        )
        mf_floor = math.ceil(self.params.mf_floor_weeks * merchant_weekly)
        allocatable = max(warehouse - mf_floor, 0)
        fba_target = math.floor(self.params.fba_cover_weeks * fba_weekly)
        wanted = fba_target - fba_on_hand - inbound
        return Allocation(
            sku=kit_group,
            warehouse_on_hand=warehouse,
            fba_on_hand=fba_on_hand,
            fba_inbound=inbound,
            mf_floor=mf_floor,
            allocatable=allocatable,
            fba_target=fba_target,
            wanted_at_fba=wanted,
            fba_send=max(min(wanted, allocatable), 0),
            walmart_reserve=self.params.walmart_reserve_units,
        )

    def _build_feasibility(self, kit_group: str) -> tuple[int, ComponentKey | None, str | None]:
        """How many of this kit could be built right now, and what runs out first."""
        kit = self.config.boms.kits[kit_group]
        buildable: int | None = None
        limiting: ComponentKey | None = None
        note: str | None = None
        for line in kit.lines:
            component = self.config.boms.components.get(line.component)
            if component is None:
                continue  # dangling reference, already reported by validate-config
            if component.component_class in (
                ComponentClass.NON_STOCKED,
                ComponentClass.OPS_CONSUMABLE,
            ):
                continue  # infinite for feasibility, or not part of assembly
            if line.channels is not None:
                continue  # prep items are consumed at packing, not at assembly
            available = self._stock_of(line.component.part).on_hand
            possible = max(available, 0) // max(line.qty, 1)
            if buildable is None or possible < buildable:
                buildable = possible
                limiting = line.component
                note = (
                    f"{component.name} ({line.component}) has {available} on hand, "
                    f"enough for {possible}"
                )
        return (buildable if buildable is not None else 0), limiting, note

    # ------------------------------------------------------------- components

    def calculate(self) -> ReplenishmentResult:
        kit_plans, kit_demand = self._kit_plans()

        fba_send_targets: dict[str, int] = {
            plan.kit_group: plan.allocation.fba_send
            for plan in kit_plans
            if plan.allocation is not None and plan.allocation.fba_send > 0
        }

        exploded: dict[ComponentKey, Fraction] = {}
        prep: dict[ComponentKey, Fraction] = {}
        for kit_group, kit in self.config.boms.kits.items():
            demand = kit_demand.get(kit_group, Fraction(0))
            send = Fraction(fba_send_targets.get(kit_group, 0))
            for line in kit.lines:
                if line.channels is None:
                    exploded[line.component] = (
                        exploded.get(line.component, Fraction(0)) + demand * line.qty
                    )
                elif any(channel in self._fba_channels for channel in line.channels):
                    prep[line.component] = prep.get(line.component, Fraction(0)) + send * line.qty

        standalone_allocations = self._standalone_allocations(fba_send_targets)
        total_fba_units = sum(fba_send_targets.values())
        for entry in self.config.boms.standalone_fba_prep:
            if entry.applies_to_all_fba_units:
                units = Fraction(total_fba_units)
            elif entry.sku is None:
                continue
            else:
                allocation = standalone_allocations.get(entry.sku)
                units = Fraction(allocation.fba_send if allocation else 0)
            prep[entry.consumes] = prep.get(entry.consumes, Fraction(0)) + units * entry.qty

        components: list[ComponentPlan] = []
        gaps: list[GapListEntry] = []
        for key, component in sorted(self.config.boms.components.items()):
            if component.component_class is ComponentClass.OPS_CONSUMABLE:
                continue  # calendar reminder only; never counted here
            plan = self._component_plan(
                key,
                component,
                exploded.get(key, Fraction(0)),
                prep.get(key, Fraction(0)),
            )
            components.append(plan)
            gap = self._gap_entry(plan)
            if gap is not None:
                gaps.append(gap)

        box_plan = plan_boxes(
            fba_send_targets,
            box_min=self.params.box_min,
            box_max=self.params.box_max,
            overship_tolerance=self.params.overship_tolerance,
        )

        return ReplenishmentResult(
            bom_version=self.config.boms.bom_version,
            parameters=self.params,
            kits=tuple(kit_plans),
            components=tuple(components),
            gap_list=tuple(gaps),
            box_plan=box_plan,
            fba_send_targets=dict(fba_send_targets),
            warnings=tuple(self.warnings),
        )

    def _standalone_allocations(self, fba_send_targets: dict[str, int]) -> dict[str, Allocation]:
        allocations: dict[str, Allocation] = {}
        for key, component in self.config.boms.components.items():
            if component.component_class is not ComponentClass.FORECAST:
                continue
            sales = self.velocity.get(key.part)
            if sales is None:
                continue
            weekly_by_channel = {
                channel: Fraction(units * 7, sales.window_days)
                for channel, units in sales.by_channel.items()
            }
            allocation = self._allocate(key.part, weekly_by_channel)
            allocations[key.part] = allocation
            if allocation.fba_send > 0:
                fba_send_targets[key.part] = allocation.fba_send
        return allocations

    def _component_plan(
        self,
        key: ComponentKey,
        component: Component,
        kit_demand: Fraction,
        prep_demand: Fraction,
    ) -> ComponentPlan:
        notes: list[str] = []
        sales = self._velocity_of(key.part)
        standalone_weekly = sales.weekly()
        horizon = self._cover(component)
        standalone_demand = standalone_weekly * horizon

        position = self._stock_of(key.part)
        on_hand = position.on_hand
        on_order = self.on_order.get(key.part, 0)
        in_transit = self._inbound_of(key.part)

        if component.component_class is ComponentClass.NON_STOCKED:
            notes.append(
                "Not stocked: present so the kit is described correctly. "
                "Purchase quantity is always zero."
            )
            return self._empty_plan(key, component, notes)

        if component.component_class is ComponentClass.REORDER_POINT:
            return self._reorder_point_plan(
                key, component, on_hand, on_order, in_transit, prep_demand, notes
            )

        safety_weeks = (
            component.safety_stock_weeks
            if component.safety_stock_weeks is not None
            else self.params.safety_stock_weeks
        )
        kit_weekly = kit_demand / horizon if horizon else Fraction(0)
        safety = safety_weeks * (standalone_weekly + kit_weekly)
        gross = standalone_demand + kit_demand + prep_demand + safety
        raw_net = gross - on_hand - on_order - in_transit
        net_units = max(ceil_fraction(raw_net), 0)
        moq_rounded = moq_round(net_units, component)
        order_units = round_up_to(moq_rounded, self.params.round_up_to_nearest)

        pack = component.units_per_purchase_unit
        if pack is None:
            purchase_units: int | None = None
            actual_units: int | None = None
            if order_units > 0:
                notes.append(
                    "Pack size has not been confirmed yet, so this line is held: "
                    "Shannon will not put a number in a cart that might mean packs."
                )
        else:
            purchase_units = math.ceil(order_units / pack) if order_units else 0
            actual_units = purchase_units * pack

        return ComponentPlan(
            key=key,
            name=component.name,
            component_class=component.component_class,
            supplier=key.supplier,
            standalone_units_sold=sales.units_sold,
            standalone_weekly=standalone_weekly,
            standalone_demand=standalone_demand,
            kit_demand=kit_demand,
            fba_prep_demand=prep_demand,
            safety_stock=safety,
            gross_demand=gross,
            on_hand=on_hand,
            on_order=on_order,
            in_transit=in_transit,
            raw_net=raw_net,
            net_units=net_units,
            moq_rounded=moq_rounded,
            order_units=order_units,
            units_per_purchase_unit=pack,
            purchase_units=purchase_units,
            actual_units=actual_units,
            purchase_unit_name=component.purchase_unit_name,
            routing=self._routing(component, order_units),
            notes=tuple(notes),
        )

    def _reorder_point_plan(
        self,
        key: ComponentKey,
        component: Component,
        on_hand: int,
        on_order: int,
        in_transit: int,
        prep_demand: Fraction,
        notes: list[str],
    ) -> ComponentPlan:
        available = on_hand + on_order + in_transit
        point = component.reorder_point
        target = component.reorder_target
        if point is None or target is None:
            notes.append(
                "No reorder point or target is set yet, so Shannon reports the "
                "stock level and leaves the decision to you."
            )
            top_up = 0
        elif available < point:
            top_up = max(target - available, 0)
        else:
            top_up = 0

        order_units = round_up_to(top_up, self.params.round_up_to_nearest)
        pack = component.units_per_purchase_unit
        if pack is None:
            purchase_units: int | None = None
            actual_units: int | None = None
            if order_units > 0:
                notes.append(
                    "Pack size has not been confirmed yet, so the quantity is shown "
                    "in sellable units only."
                )
        else:
            purchase_units = math.ceil(order_units / pack) if order_units else 0
            actual_units = purchase_units * pack

        if prep_demand > 0:
            notes.append(
                f"{ceil_fraction(prep_demand)} of these are consumed by this week's FBA prep."
            )

        return ComponentPlan(
            key=key,
            name=component.name,
            component_class=component.component_class,
            supplier=key.supplier,
            standalone_units_sold=0,
            standalone_weekly=Fraction(0),
            standalone_demand=Fraction(0),
            kit_demand=Fraction(0),
            fba_prep_demand=prep_demand,
            safety_stock=Fraction(0),
            gross_demand=Fraction(target or 0),
            on_hand=on_hand,
            on_order=on_order,
            in_transit=in_transit,
            raw_net=Fraction(top_up),
            net_units=top_up,
            moq_rounded=top_up,
            order_units=order_units,
            units_per_purchase_unit=pack,
            purchase_units=purchase_units,
            actual_units=actual_units,
            purchase_unit_name=component.purchase_unit_name,
            routing=self._routing(component, order_units),
            reorder_point=point,
            reorder_target=target,
            available=available,
            notes=tuple(notes),
        )

    def _empty_plan(
        self, key: ComponentKey, component: Component, notes: list[str]
    ) -> ComponentPlan:
        return ComponentPlan(
            key=key,
            name=component.name,
            component_class=component.component_class,
            supplier=key.supplier,
            standalone_units_sold=0,
            standalone_weekly=Fraction(0),
            standalone_demand=Fraction(0),
            kit_demand=Fraction(0),
            fba_prep_demand=Fraction(0),
            safety_stock=Fraction(0),
            gross_demand=Fraction(0),
            on_hand=0,
            on_order=0,
            in_transit=0,
            raw_net=Fraction(0),
            net_units=0,
            moq_rounded=0,
            order_units=0,
            units_per_purchase_unit=component.units_per_purchase_unit,
            purchase_units=0,
            actual_units=0,
            purchase_unit_name=component.purchase_unit_name,
            routing=NO_PURCHASE,
            notes=tuple(notes),
        )

    def _routing(self, component: Component, order_units: int) -> str:
        if component.component_class is ComponentClass.NON_STOCKED:
            return NO_PURCHASE
        supplier = self.config.boms.suppliers.get(component.key.supplier)
        if supplier is None:
            return GAP_LIST
        if order_units <= 0:
            return NO_PURCHASE
        if supplier.can(Capability.STAGE_CART):
            return f"{supplier.key}{CART_SUFFIX}"
        return GAP_LIST

    def _gap_entry(self, plan: ComponentPlan) -> GapListEntry | None:
        if plan.routing != GAP_LIST:
            return None
        supplier = self.config.boms.suppliers.get(plan.supplier)
        reason = (
            f"{supplier.name} has no way for Shannon to order"
            if supplier is not None
            else "This supplier is not configured"
        )
        return GapListEntry(
            key=plan.key,
            name=plan.name,
            supplier=plan.supplier,
            available=plan.available if plan.available is not None else plan.on_hand,
            threshold=plan.reorder_point,
            suggested_top_up=plan.order_units,
            reason=f"{reason}; order this by hand.",
        )


__all__ = [
    "Allocation",
    "ComponentPlan",
    "GapListEntry",
    "KitPlan",
    "ReplenishmentCalculator",
    "ReplenishmentResult",
    "moq_round",
    "round_up_to",
]
