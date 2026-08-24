"""Typed configuration objects.

Everything downstream of the loader works with these, never with raw
dictionaries, so a missing field is a load-time failure rather than a
`KeyError` halfway through a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction

from agent_org.config.errors import Finding
from agent_org.config.listings import EMPTY, ListingsConfig
from agent_org.config.yamlsource import Loc


class ComponentClass(str, Enum):
    """The class decides whether a component can be bought at all."""

    FORECAST = "forecast"
    REORDER_POINT = "reorder_point"
    NON_STOCKED = "non_stocked"
    OPS_CONSUMABLE = "ops_consumable"


class Capability(str, Enum):
    READ_CATALOG = "read_catalog"
    READ_ORDER_HISTORY = "read_order_history"
    STAGE_CART = "stage_cart"
    PURCHASE = "purchase"
    REPORT_ONLY = "report_only"
    PENDING = "pending"


@dataclass(frozen=True, order=True)
class ComponentKey:
    """Component identity is (supplier, supplier part number). Always both."""

    supplier: str
    part: str

    def __str__(self) -> str:
        return f"{self.supplier}/{self.part}"


@dataclass(frozen=True)
class Supplier:
    key: str
    name: str
    acquisition: str
    capabilities: frozenset[Capability]
    lead_time_weeks: Fraction | None
    # A supplier slower than the global cover target needs its own target: the
    # cover target is inclusive of lead time, so 7 weeks of cover cannot buy
    # from a supplier that takes 9 weeks to deliver.
    cover_target_weeks: Fraction | None
    loc: Loc

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class Component:
    key: ComponentKey
    name: str
    component_class: ComponentClass
    units_per_purchase_unit: int | None
    purchase_unit_name: str | None
    moq_min: int
    moq_increment: int
    reorder_point: int | None
    reorder_target: int | None
    purchase_asin: str | None
    # The listing Zach sells this component on. Descriptive only: sales join
    # on the channel SKU, which Zach controls (docs/replenishment.md §5).
    sales_asin: str | None
    # True where the supplier genuinely publishes no item number — Orca sells
    # by product name. The part is then ours, held for identity only, and is
    # never quoted to the supplier as a SKU.
    part_is_internal_reference: bool
    # True for a finished product Zach buys complete and resells as it comes:
    # forecast from its own sales, never looked for inside a kit. Not being
    # used by any kit is its normal state, not a gap in the parts list.
    resale_only: bool
    cover_target_weeks: Fraction | None
    safety_stock_weeks: Fraction | None
    loc: Loc

    @property
    def supplier(self) -> str:
        return self.key.supplier

    @property
    def order_by(self) -> str:
        """What goes on a purchase order: a real part number, or the name."""
        return self.name if self.part_is_internal_reference else self.key.part


def cover_target_for(
    component: Component | None, supplier: Supplier | None, default: Fraction
) -> Fraction:
    """Weeks of cover for one component: the component's own figure wins, then
    its supplier's, then the entity default."""
    if component is not None and component.cover_target_weeks is not None:
        return component.cover_target_weeks
    if supplier is not None and supplier.cover_target_weeks is not None:
        return supplier.cover_target_weeks
    return default


@dataclass(frozen=True)
class BomLine:
    component: ComponentKey
    qty: int
    channels: frozenset[str] | None  # None = consumed on every channel
    loc: Loc


@dataclass(frozen=True)
class Kit:
    kit_group: str
    name: str
    # Colourways of one product are forecast, built and shipped together, so
    # they share a family. A kit with no family stated is its own family.
    family: str
    family_name: str | None
    # channel key -> channel SKU. None means the file says TODO: unresolved,
    # reported by validate-config, never guessed at.
    aliases: dict[str, str | None]
    lines: tuple[BomLine, ...]
    build_blocked: str | None
    loc: Loc


@dataclass(frozen=True)
class StandaloneFbaPrep:
    sku: str | None
    applies_to_all_fba_units: bool
    consumes: ComponentKey
    qty: int
    loc: Loc


@dataclass(frozen=True)
class ParkingLotItem:
    id: str
    item: str
    # A settled question is not something Zach still has to deal with, so it
    # leaves the live list and is kept in a closed section for the record.
    resolved: bool
    detail: str | None
    blocks: str | None
    loc: Loc


@dataclass(frozen=True)
class PackSizePolicy:
    mode: str
    on_mismatch: str


@dataclass(frozen=True)
class BomConfig:
    bom_version: str
    pack_size_policy: PackSizePolicy
    suppliers: dict[str, Supplier]
    components: dict[ComponentKey, Component]
    kits: dict[str, Kit]
    standalone_fba_prep: tuple[StandaloneFbaPrep, ...]
    parking_lot: tuple[ParkingLotItem, ...]


@dataclass(frozen=True)
class Parameters:
    """Replenishment parameters (docs/replenishment.md §9).

    None of these are constants in code; every one is read from
    `config/<entity>/shannon.yaml` and printed on every report.
    """

    velocity_window_days: int
    cover_target_weeks: Fraction
    safety_stock_weeks: Fraction
    round_up_to_nearest: int
    mf_floor_weeks: Fraction
    fba_cover_weeks: Fraction
    walmart_reserve_units: int
    box_min: int
    box_max: int
    overship_tolerance: int
    task_wall_clock_budget_seconds: int
    task_step_budget: int


@dataclass(frozen=True)
class Recipient:
    role: str
    email: str | None
    sms: str | None
    loc: Loc


@dataclass(frozen=True)
class OpsReminderGroup:
    name: str
    recipients: tuple[str, ...]
    components: tuple[ComponentKey, ...]
    loc: Loc


@dataclass(frozen=True)
class ShannonConfig:
    from_name: str
    from_address: str
    recipients: dict[str, Recipient]
    parameters: Parameters
    ops_reminder_cadence_weeks: int
    ops_reminder_groups: tuple[OpsReminderGroup, ...]


@dataclass(frozen=True)
class Channel:
    name: str
    key: str  # short name used in boms.yaml aliases and `channels:` filters
    fulfillment: str  # fba | merchant | wfs
    has_history: bool


@dataclass(frozen=True)
class AgentSchedule:
    name: str
    kind: str
    schedule: str


@dataclass(frozen=True)
class EntityConfig:
    entity_id: str
    legal_name: str
    status: str
    timezone: str
    channels: tuple[Channel, ...]
    agents: tuple[AgentSchedule, ...]
    credentials_prefix: str
    suppliers_config: str
    boms_config: str
    shannon_config: str
    policy_config: str
    listings_config: str = ""

    def channel(self, name: str) -> Channel | None:
        for channel in self.channels:
            if name in (channel.name, channel.key):
                return channel
        return None

    @property
    def channel_keys(self) -> tuple[str, ...]:
        return tuple(channel.key for channel in self.channels)


@dataclass(frozen=True)
class PolicyRule:
    action: str
    tier: int


@dataclass(frozen=True)
class PolicyConfig:
    version: int
    default_tier: int
    max_tier_this_phase: int
    rules: dict[str, PolicyRule]
    thresholds: dict[str, object]
    escalations: tuple[dict[str, object], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LoadedConfig:
    entity: EntityConfig
    boms: BomConfig
    shannon: ShannonConfig
    policy: PolicyConfig
    listings: ListingsConfig = EMPTY
    findings: tuple[Finding, ...] = ()

    @property
    def entity_id(self) -> str:
        return self.entity.entity_id
