"""What Shannon is allowed to know, and the shape it arrives in.

These are read-only data structures and protocols. There is no write
method anywhere in this module by design: in this phase nothing can change
a number in Veeqo, Shopify or Amazon because no code exists that could.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Protocol, runtime_checkable

# Veeqo warehouse identifiers (iThrive — see the NAR reorder procedure).
SPRINGFIELD_WAREHOUSE_ID = 70459
AMAZON_US_FBA_WAREHOUSE_ID = 192025


class ReadFailure(RuntimeError):
    """A data source could not be read. The run stops; it never guesses."""


class AmbiguousOrderSignal(ReadFailure):
    """Gmail cannot say what is still on order.

    Double-ordering is the most expensive failure mode in this system, so
    an unclear signal stops the run and asks Zach, rather than assuming.
    """


@dataclass(frozen=True)
class StockPosition:
    """One sellable SKU's stock, in sellable units.

    `available` keeps its sign: a negative number means more is committed
    to orders than is physically on the shelf — a real backlog, not a
    rounding artefact, and never clamped to zero.
    """

    sku: str
    warehouse_available: int
    fba_sellable: int
    fba_reserved: int = 0  # held here for the report only; never counted as stock
    fba_unfulfillable: int = 0

    @property
    def on_hand(self) -> int:
        """Everything Shannon may count: warehouse plus FBA *sellable* only."""
        return self.warehouse_available + self.fba_sellable


@dataclass(frozen=True)
class SalesVelocity:
    """Units sold in the trailing window, per SKU, split by sales channel.

    The split matters only for allocation (§7); demand uses the total. A
    channel with no sales is simply absent, which is how Walmart behaves
    until it has history.
    """

    sku: str
    units_sold: int
    window_days: int
    by_channel: Mapping[str, int] = field(default_factory=dict)

    def weekly(self) -> Fraction:
        """units_sold ÷ (window ÷ 7) — kept exact, never rounded early."""
        return Fraction(self.units_sold * 7, self.window_days)

    def weekly_on(self, channel: str) -> Fraction:
        return Fraction(self.by_channel.get(channel, 0) * 7, self.window_days)


@dataclass(frozen=True)
class InboundShipment:
    sku: str
    units: int
    expected_at: datetime | None = None


@dataclass(frozen=True)
class OrderSignals:
    """What Gmail says is still outstanding with a supplier."""

    on_order: dict[str, int]
    outstanding_orders: tuple[str, ...] = ()
    split_shipment_flags: tuple[str, ...] = ()
    ignored_directives: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())


class InventoryReader(Protocol):
    def read_inventory(self) -> dict[str, StockPosition]: ...

    def read_velocity(self, window_days: int) -> dict[str, SalesVelocity]: ...

    def read_fba_inbound(self) -> dict[str, InboundShipment]: ...


@runtime_checkable
class HistoricalVelocityReader(Protocol):
    """A longer window than the forecast one, for suppressed lines only.

    When a listing has been down for months the trailing 90 days say zero,
    which is true about the listing and false about the demand. A source
    that can look further back offers this; one that cannot simply does
    not, and Shannon says the history does not reach.
    """

    def read_velocity_history(self) -> dict[str, SalesVelocity]: ...


class OrderSignalReader(Protocol):
    def read_order_signals(self) -> OrderSignals: ...


__all__ = [
    "AMAZON_US_FBA_WAREHOUSE_ID",
    "SPRINGFIELD_WAREHOUSE_ID",
    "AmbiguousOrderSignal",
    "HistoricalVelocityReader",
    "InboundShipment",
    "InventoryReader",
    "OrderSignalReader",
    "OrderSignals",
    "ReadFailure",
    "SalesVelocity",
    "StockPosition",
]
