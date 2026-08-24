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
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from agent_org.config.listings import ListingSet
from agent_org.config.models import (
    Capability,
    Component,
    ComponentClass,
    ComponentKey,
    Kit,
    LoadedConfig,
    Parameters,
    cover_target_for,
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
class KitBuild:
    """One colourway inside a kit family: what can be built, and what runs out."""

    kit_group: str
    name: str
    buildable_now: int
    limiting_component: ComponentKey | None
    limiting_note: str | None
    build_blocked: str | None
    # The family's build recommendation, divided by this colourway's own
    # demand and its own assembled stock. The shares always sum to the
    # family total: one number to build, and where it lands.
    demand_units: int = 0
    assembled_stock: int = 0
    build_share: int = 0


def _split_build(builds: list[KitBuild], family_build: int) -> list[KitBuild]:
    """Divide one family build recommendation across its colourways.

    Each colourway's own shortfall (its demand less its own stock) comes
    first; whatever the family total leaves over is handed out in demand
    order. The shares always add up to the family figure, so the report can
    show the split without the two views disagreeing.
    """
    if not builds:
        return builds
    shortfalls = [max(item.demand_units - item.assembled_stock, 0) for item in builds]
    total = sum(shortfalls)
    if total == 0:
        shares = [0] * len(builds)
        shares[0] = family_build
    elif total <= family_build:
        shares = list(shortfalls)
        # Spread the remainder over the colourways that sell fastest.
        order = sorted(range(len(builds)), key=lambda i: -builds[i].demand_units)
        left = family_build - total
        for position, index in enumerate(order):
            shares[index] += left // len(builds) + (1 if position < left % len(builds) else 0)
    else:
        shares = [shortfall * family_build // total for shortfall in shortfalls]
        order = sorted(
            range(len(builds)),
            key=lambda i: -((shortfalls[i] * family_build) % total),
        )
        for index in order[: family_build - sum(shares)]:
            shares[index] += 1
    return [
        KitBuild(
            kit_group=item.kit_group,
            name=item.name,
            buildable_now=item.buildable_now,
            limiting_component=item.limiting_component,
            limiting_note=item.limiting_note,
            build_blocked=item.build_blocked,
            demand_units=item.demand_units,
            assembled_stock=item.assembled_stock,
            build_share=share,
        )
        for item, share in zip(builds, shares, strict=True)
    ]


@dataclass(frozen=True)
class KitPlan:
    """A kit family: colourways are forecast, built and shipped together."""

    family: str
    name: str
    weekly_velocity: Fraction
    demand_units: int
    assembled_stock: int
    build_recommendation: int
    members: tuple[KitBuild, ...]
    allocation: Allocation | None
    unresolved_aliases: tuple[str, ...] = ()

    @property
    def buildable_now(self) -> int:
        return sum(member.buildable_now for member in self.members)


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
    # True where the part number is ours because the supplier publishes none.
    # Such a line is ordered by name; printing the reference as if it were a
    # supplier SKU is how a purchase order ends up quoting an invented number.
    part_is_internal_reference: bool = False
    # Descriptive only, so a human recognises the listing. Never a join key.
    sales_asins: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def order_by(self) -> str:
        """What to write on a purchase order for this line."""
        return self.name if self.part_is_internal_reference else self.key.part


@dataclass(frozen=True)
class SuppressedDemand:
    """A line whose sales figure measures the listing, not the demand.

    Zach deactivates a listing when he is out of stock, which sends sales
    to zero, which sends a trailing average to zero, which would order
    nothing and keep him out of stock. Shannon will not forecast such a
    line: she says so, gives whatever history reaches back past the
    deactivation, and puts the decision in front of Zach.
    """

    subject: str
    name: str
    kind: str  # "kit" or "component"
    channels: tuple[str, ...]
    current_weekly: Fraction
    historical_weekly: Fraction | None
    historical_window_days: int | None

    @property
    def has_history(self) -> bool:
        return self.historical_weekly is not None


@dataclass(frozen=True)
class ParkingLotAddition:
    """Something only Zach can decide, raised by this run rather than by config."""

    id: str
    item: str
    detail: str
    blocks: str


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
    suppressed: tuple[SuppressedDemand, ...] = ()
    parking_lot_additions: tuple[ParkingLotAddition, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def component(self, supplier: str, part: str) -> ComponentPlan:
        key = ComponentKey(supplier=supplier, part=part)
        for plan in self.components:
            if plan.key == key:
                return plan
        raise KeyError(f"No line for {key} in this run.")

    def kit(self, family: str) -> KitPlan:
        for plan in self.kits:
            if plan.family == family:
                return plan
        raise KeyError(f"No kit plan for {family} in this run.")


def _parking_lot_for(suppressed: Sequence[SuppressedDemand]) -> tuple[ParkingLotAddition, ...]:
    """Suppressed lines go straight to the parking lot: only Zach can decide
    whether to restock and relist, and the decision must not wait for him to
    notice a missing line."""
    return tuple(
        ParkingLotAddition(
            id=f"AUTO-{item.subject}",
            item=f"Decide whether to restock and relist {item.subject} ({item.name})",
            detail=(
                "Every listing is inactive, so recent sales measure the listing "
                "rather than the demand. Shannon will not forecast it."
            ),
            blocks=f"Any reorder of {item.subject}",
        )
        for item in suppressed
    )


class ReplenishmentCalculator:
    def __init__(
        self,
        config: LoadedConfig,
        stock: dict[str, StockPosition],
        velocity: dict[str, SalesVelocity],
        inbound: dict[str, InboundShipment],
        on_order: dict[str, int],
        historical_velocity: dict[str, SalesVelocity] | None = None,
    ) -> None:
        self.config = config
        self.stock = stock
        self.velocity = velocity
        self.inbound = inbound
        self.on_order = on_order
        # A longer window than the forecast one, used for nothing except
        # saying what a suppressed line used to sell. It never feeds a
        # forecast: a suppressed line is surfaced, never predicted.
        self.historical_velocity = historical_velocity or {}
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

    def _sum_velocity(
        self,
        skus: Sequence[str],
        label: str,
        source: dict[str, SalesVelocity] | None = None,
    ) -> SalesVelocity | None:
        """Add up the sales of several channel SKUs into one figure.

        One component can be listed several times — the C-A-T Gen 7 in
        orange has two FBA SKUs and an FBM one — and several channel SKUs
        can share an ASIN. Its demand is the sum; taking any single listing
        would under-order it.
        """
        rows = source if source is not None else self.velocity
        found = [rows[sku] for sku in skus if sku in rows]
        if not found:
            return None
        by_channel: dict[str, int] = {}
        for row in found:
            for channel, units in row.by_channel.items():
                by_channel[channel] = by_channel.get(channel, 0) + units
        return SalesVelocity(
            sku=label,
            units_sold=sum(row.units_sold for row in found),
            window_days=found[0].window_days,
            by_channel=by_channel,
        )

    def _standalone_velocity(self, key: ComponentKey, component: Component) -> SalesVelocity:
        """Standalone sales of a component.

        The join is the channel SKU, which is Zach's own and which Veeqo
        keys on. The ASIN is not: NAR owns the C-A-T listings, three
        colourways share them, and no title states a colour, so an ASIN
        cannot say which product sold. It stays in the report for a human
        to recognise and is never the key (docs/replenishment.md §5).

        Only the sales side drives demand: a `purchase_asin` says nothing
        about how many Zach sells (docs/replenishment.md §10 step 2).
        """
        listing_set = self.config.listings.for_part(key.part)
        if listing_set is not None and listing_set.channel_skus:
            # Mapped: the sum over its channel SKUs is the answer, even when
            # that sum is nothing. Reaching past it to an ASIN here is what
            # would merge three colourways into one line.
            summed = self._sum_velocity(listing_set.channel_skus, key.part)
            if summed is not None:
                return summed
            return SalesVelocity(
                sku=key.part, units_sold=0, window_days=self.params.velocity_window_days
            )
        if component.sales_asin is not None and component.sales_asin in self.velocity:
            # Unmapped, so the ASIN is all there is. It is a weaker key and
            # only safe while nothing else claims it.
            return self.velocity[component.sales_asin]
        return self._velocity_of(key.part)

    def _sales_asins(self, key: ComponentKey, component: Component) -> tuple[str, ...]:
        """Every ASIN this component is listed under, for recognition only."""
        listing_set = self.config.listings.for_part(key.part)
        asins = list(listing_set.sales_asins) if listing_set is not None else []
        if component.sales_asin is not None and component.sales_asin not in asins:
            asins.append(component.sales_asin)
        return tuple(asins)

    def _inbound_of(self, sku: str) -> int:
        shipment = self.inbound.get(sku)
        return shipment.units if shipment else 0

    def _cover(self, component: Component | None) -> Fraction:
        supplier = (
            self.config.boms.suppliers.get(component.key.supplier)
            if component is not None
            else None
        )
        return cover_target_for(component, supplier, self.params.cover_target_weeks)

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

    def _kit_skus(self, kit: Kit) -> tuple[tuple[str, str], ...]:
        """(channel, SKU) pairs, one per distinct SKU.

        A kit normally carries the same SKU on FBM and Shopify. Counting its
        stock or its sales once per alias would double the kit.
        """
        first_channel: dict[str, str] = {}
        for channel, sku in sorted(kit.aliases.items()):
            if sku is not None and sku not in first_channel:
                first_channel[sku] = channel
        return tuple((channel, sku) for sku, channel in first_channel.items())

    def _kit_sales_skus(self, kit: Kit) -> tuple[tuple[str, str], ...]:
        """(channel, SKU) pairs to read sales against.

        Amazon holds its own SKU for a kit and it looks nothing like the
        internal one, so where listings.yaml names it, that is the join.
        Channels it says nothing about — Shopify — keep the internal SKU.
        Stock is a different question and stays on the internal SKU: that
        is what Veeqo counts in the warehouse.
        """
        listing_set = self.config.listings.for_kit(kit.kit_group)
        if listing_set is None:
            return self._kit_skus(kit)
        pairs = [
            (listing.channel, listing.sku)
            for listing in listing_set.listings
            if listing.sku in self.velocity
        ]
        if not pairs:
            return self._kit_skus(kit)
        covered = {channel for channel, _ in pairs}
        pairs += [(channel, sku) for channel, sku in self._kit_skus(kit) if channel not in covered]
        return tuple(pairs)

    def _kit_channel_velocity(self, kit: Kit) -> dict[str, Fraction]:
        weekly: dict[str, Fraction] = {}
        for channel_key, sku in self._kit_sales_skus(kit):
            sales = self._velocity_of(sku)
            if sales.by_channel:
                for channel, units in sales.by_channel.items():
                    weekly[channel] = weekly.get(channel, Fraction(0)) + Fraction(
                        units * 7, sales.window_days
                    )
            else:
                weekly[channel_key] = weekly.get(channel_key, Fraction(0)) + sales.weekly()
        return weekly

    def _families(self) -> dict[str, list[Kit]]:
        families: dict[str, list[Kit]] = {}
        for _, kit in sorted(self.config.boms.kits.items()):
            families.setdefault(kit.family, []).append(kit)
        return families

    def _family_name(self, family: str, members: list[Kit]) -> str:
        for kit in members:
            if kit.family_name:
                return kit.family_name
        if len(members) == 1:
            return members[0].name
        return family

    def _kit_plans(self) -> tuple[list[KitPlan], dict[str, Fraction]]:
        """Family demand, build recommendations and allocation, in that order."""
        plans: list[KitPlan] = []
        kit_demand: dict[str, Fraction] = {}
        horizon = self.params.cover_target_weeks

        for family, members in sorted(self._families().items()):
            weekly_by_channel: dict[str, Fraction] = {}
            warehouse = 0
            fba_on_hand = 0
            inbound = 0
            has_fba_alias = False
            unresolved: list[str] = []
            builds: list[KitBuild] = []

            for kit in members:
                per_kit = self._kit_channel_velocity(kit)
                for channel, value in per_kit.items():
                    weekly_by_channel[channel] = weekly_by_channel.get(channel, Fraction(0)) + value
                member_demand = sum(per_kit.values(), Fraction(0)) * horizon
                kit_demand[kit.kit_group] = member_demand
                member_stock = 0
                listing_set = self.config.listings.for_kit(kit.kit_group)
                # Whether a kit sells on FBA is a fact about its listings, not
                # about which internal alias happens to be filled in.
                on_fba = any(
                    listing.channel in self._fba_channels and listing.is_active
                    for listing in (listing_set.listings if listing_set is not None else ())
                )
                for channel, sku in self._kit_skus(kit):
                    position = self._stock_of(sku)
                    warehouse += position.warehouse_available
                    fba_on_hand += position.fba_sellable
                    member_stock += position.warehouse_available + position.fba_sellable
                    if channel in self._fba_channels or on_fba:
                        has_fba_alias = True
                        inbound += self._inbound_of(sku)
                unresolved.extend(
                    f"{kit.kit_group} has no {channel} SKU"
                    for channel, sku in sorted(kit.aliases.items())
                    # listings.yaml is the authority on Amazon identity: where it
                    # covers a kit, an internal TODO alias is not a gap (PL-8).
                    if sku is None and listing_set is None
                )
                buildable, limiting, limiting_note = self._build_feasibility(kit)
                builds.append(
                    KitBuild(
                        kit_group=kit.kit_group,
                        name=kit.name,
                        buildable_now=buildable,
                        limiting_component=limiting,
                        limiting_note=limiting_note,
                        build_blocked=kit.build_blocked,
                        demand_units=ceil_fraction(member_demand),
                        assembled_stock=member_stock,
                    )
                )

            weekly = sum(weekly_by_channel.values(), Fraction(0))
            demand_units = ceil_fraction(weekly * horizon)
            assembled = warehouse + fba_on_hand
            build = max(demand_units - assembled, 0)
            buildable_total = sum(item.buildable_now for item in builds)
            builds = _split_build(builds, build)
            name = self._family_name(family, members)

            allocation: Allocation | None = None
            if has_fba_alias:
                allocation = self._allocate_family(
                    family, weekly_by_channel, warehouse, fba_on_hand, inbound
                )

            if build > buildable_total:
                short = ", ".join(
                    item.limiting_note
                    for item in builds
                    if item.limiting_note is not None and item.buildable_now == 0
                )
                self.warnings.append(
                    f"{name}: {build} to build but only {buildable_total} can be "
                    f"assembled from stock on hand" + (f" — {short}" if short else "") + "."
                )

            plans.append(
                KitPlan(
                    family=family,
                    name=name,
                    weekly_velocity=weekly,
                    demand_units=demand_units,
                    assembled_stock=assembled,
                    build_recommendation=build,
                    members=tuple(builds),
                    allocation=allocation,
                    unresolved_aliases=tuple(unresolved),
                )
            )
        return plans, kit_demand

    def _allocate_family(
        self,
        family: str,
        weekly_by_channel: dict[str, Fraction],
        warehouse: int,
        fba_on_hand: int,
        inbound: int,
    ) -> Allocation:
        """Allocation spans a family: colourways ship in one shipment."""
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
            sku=family,
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

    def _build_feasibility(self, kit: Kit) -> tuple[int, ComponentKey | None, str | None]:
        """How many of this kit could be built right now, and what runs out first."""
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
            plan.family: plan.allocation.fba_send
            for plan in kit_plans
            if plan.allocation is not None and plan.allocation.fba_send > 0
        }
        family_demand = {
            plan.family: sum(
                (kit_demand.get(member.kit_group, Fraction(0)) for member in plan.members),
                Fraction(0),
            )
            for plan in kit_plans
        }

        exploded: dict[ComponentKey, Fraction] = {}
        prep: dict[ComponentKey, Fraction] = {}
        for kit_group, kit in self.config.boms.kits.items():
            demand = kit_demand.get(kit_group, Fraction(0))
            # A family ships as one shipment, so its send quantity is split
            # across colourways in proportion to their demand.
            family_total = family_demand.get(kit.family, Fraction(0))
            share = demand / family_total if family_total else Fraction(0)
            send = Fraction(fba_send_targets.get(kit.family, 0)) * share
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

        suppressed = self._suppressed_demand(kit_plans)

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
            suppressed=suppressed,
            parking_lot_additions=_parking_lot_for(suppressed),
            warnings=tuple(self.warnings),
        )

    # ----------------------------------------------------- demand suppression

    def _historical_weekly(self, listing_set: ListingSet, fallback: str) -> SalesVelocity | None:
        """What this sold before the listing came down, where the history
        reaches back that far. Never substituted for a forecast."""
        if not self.historical_velocity:
            return None
        summed = self._sum_velocity(
            listing_set.channel_skus, listing_set.subject, self.historical_velocity
        )
        if summed is None:
            summed = self._sum_velocity([fallback], fallback, self.historical_velocity)
        if summed is None or summed.units_sold <= 0:
            return None
        return summed

    def _suppressed_demand(self, kit_plans: list[KitPlan]) -> tuple[SuppressedDemand, ...]:
        """Every kit and component no customer can currently buy.

        Out of stock → listing deactivated → sales zero → velocity zero →
        nothing ordered → still out of stock. Breaking that loop is the
        only reason this exists: such a line is reported as suppressed,
        with whatever it used to sell, and never as a zero-demand item.
        """
        found: list[SuppressedDemand] = []
        weekly_by_kit: dict[str, Fraction] = {}
        for plan in kit_plans:
            for member in plan.members:
                kit = self.config.boms.kits.get(member.kit_group)
                if kit is not None:
                    weekly_by_kit[member.kit_group] = sum(
                        self._kit_channel_velocity(kit).values(), Fraction(0)
                    )
        for kit_group, listing_set in sorted(self.config.listings.kits.items()):
            if not listing_set.demand_is_suppressed:
                continue
            kit = self.config.boms.kits.get(kit_group)
            history = self._historical_weekly(listing_set, kit_group)
            found.append(
                SuppressedDemand(
                    subject=kit_group,
                    name=kit.name if kit is not None else kit_group,
                    kind="kit",
                    channels=tuple(
                        f"{listing.channel} {listing.sku}" for listing in listing_set.listings
                    ),
                    current_weekly=weekly_by_kit.get(kit_group, Fraction(0)),
                    historical_weekly=history.weekly() if history is not None else None,
                    historical_window_days=history.window_days if history is not None else None,
                )
            )
        for part, listing_set in sorted(self.config.listings.components.items()):
            if not listing_set.demand_is_suppressed:
                continue
            component = next(
                (
                    item
                    for key, item in sorted(self.config.boms.components.items())
                    if key.part == part
                ),
                None,
            )
            history = self._historical_weekly(listing_set, part)
            found.append(
                SuppressedDemand(
                    subject=part,
                    name=component.name if component is not None else part,
                    kind="component",
                    channels=tuple(
                        f"{listing.channel} {listing.sku}" for listing in listing_set.listings
                    ),
                    current_weekly=self._standalone_velocity(component.key, component).weekly()
                    if component is not None
                    else Fraction(0),
                    historical_weekly=history.weekly() if history is not None else None,
                    historical_window_days=history.window_days if history is not None else None,
                )
            )
        return tuple(found)

    def _standalone_allocations(self, fba_send_targets: dict[str, int]) -> dict[str, Allocation]:
        allocations: dict[str, Allocation] = {}
        for key, component in self.config.boms.components.items():
            if component.component_class is not ComponentClass.FORECAST:
                continue
            sales = self._standalone_velocity(key, component)
            if not sales.by_channel:
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
        sales = self._standalone_velocity(key, component)
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
            part_is_internal_reference=component.part_is_internal_reference,
            sales_asins=self._sales_asins(key, component),
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
            part_is_internal_reference=component.part_is_internal_reference,
            sales_asins=self._sales_asins(key, component),
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
            part_is_internal_reference=component.part_is_internal_reference,
            sales_asins=self._sales_asins(key, component),
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
