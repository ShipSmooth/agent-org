"""Veeqo — read only, from the live API or from fixtures on disk.

Two facts from the reorder procedure are encoded here rather than left to
whoever reads the report:

* The products report prints two numbers in one cell — the current window
  and the comparison window, e.g. "450 (390)". The first is the one that
  means anything for this week. `first_value` takes it.
* Negative availability is real. A SKU at -12 is twelve units short
  against orders already placed; clamping that to zero would quietly
  under-order by twelve.

What the live API actually provides, which is not what Phase 1 guessed:

* Stock: `GET /products`, paginated. Each product holds `sellables`, each
  sellable holds `sku_code` and one `stock_entries` row per warehouse,
  with `physical_stock_level`, `available_stock_level`,
  `allocated_stock_level`, `incoming_stock_level` and
  `infinite`. Per-SKU-per-location, as required, and the FBA warehouse is
  one of those rows rather than a separate feed.
* Velocity: `GET /orders`, paginated, filtered by `created_at_min` /
  `created_at_max`, each order carrying `created_at`, `status`, `channel`
  and `line_items` whose `sellable.sku_code` and `quantity` are the units
  sold. There is no per-channel sales-history report endpoint: the split
  by channel is computed here from each order's channel, not read.
* History for a suppressed line is the same `GET /orders` call over an
  earlier window. There is no separate history export.

There is no credential in this file. `VEEQO_API_KEY` is read from the
environment at the moment of use, and a missing one stops the run.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from agent_org.integrations.reads import (
    AMAZON_US_FBA_WAREHOUSE_ID,
    InboundShipment,
    ReadFailure,
    SalesVelocity,
    StockPosition,
)

NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

VEEQO_API_KEY_VAR = "VEEQO_API_KEY"
VEEQO_BASE_URL = "https://api.veeqo.com"
PAGE_SIZE = 100
# Veeqo counts a cancelled order as an order. Counting one as a sale would
# forecast demand that never existed.
NOT_A_SALE = frozenset({"cancelled", "canceled", "refunded"})


def first_value(cell: object) -> int:
    """Read the first number out of a Veeqo report cell.

    "450 (390)" is one cell holding this window and the comparison window.
    Reading it as 450390, or as the second number, has been a real source
    of wrong orders.
    """
    if isinstance(cell, bool):
        raise ReadFailure(f"Expected a number in the Veeqo report, found {cell!r}.")
    if isinstance(cell, int):
        return cell
    if isinstance(cell, float):
        return int(cell)
    match = NUMBER.search(str(cell))
    if match is None:
        raise ReadFailure(f"Could not read a number out of the Veeqo report cell {cell!r}.")
    return int(float(match.group(0)))


@dataclass(frozen=True)
class VeeqoFixtureClient:
    """A Veeqo client that reads a folder of saved report exports.

    Same interface a live client will have. Nothing here can write to
    Veeqo, and no credential is used or required.
    """

    fixture_dir: Path

    def _load(self, name: str) -> Any:
        path = self.fixture_dir / name
        if not path.exists():
            raise ReadFailure(
                f"Veeqo fixture '{path}' is missing, so stock cannot be read. "
                "The run stops rather than assuming a number."
            )
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def read_inventory(self) -> dict[str, StockPosition]:
        data = self._load("inventory.json")
        positions: dict[str, StockPosition] = {}
        for row in data["products"]:
            sku = str(row["sku"])
            warehouses = {int(k): first_value(v) for k, v in row.get("warehouses", {}).items()}
            fba = row.get("fba", {})
            positions[sku] = StockPosition(
                sku=sku,
                warehouse_available=sum(
                    units for wid, units in warehouses.items() if wid != AMAZON_US_FBA_WAREHOUSE_ID
                ),
                fba_sellable=first_value(fba.get("sellable", 0)),
                fba_reserved=first_value(fba.get("reserved", 0)),
                fba_unfulfillable=first_value(fba.get("unfulfillable", 0)),
            )
        return positions

    def read_velocity(self, window_days: int) -> dict[str, SalesVelocity]:
        data = self._load("velocity.json")
        fixture_window = int(data.get("window_days", window_days))
        if fixture_window != window_days:
            raise ReadFailure(
                f"The Veeqo sales report covers {fixture_window} days but the run "
                f"asked for {window_days}. Velocities would be wrong, so the run stops."
            )
        velocities: dict[str, SalesVelocity] = {}
        for row in data["rows"]:
            sku = str(row["sku"])
            total = first_value(row["units_sold"])
            by_channel = {
                str(channel): first_value(units)
                for channel, units in row.get("by_channel", {}).items()
            }
            if by_channel and sum(by_channel.values()) != total:
                raise ReadFailure(
                    f"The sales report for {sku} says {total} units sold, but the "
                    f"per-channel figures add up to {sum(by_channel.values())}. "
                    "The run stops rather than pick one of them."
                )
            velocities[sku] = SalesVelocity(
                sku=sku,
                units_sold=total,
                window_days=window_days,
                by_channel=by_channel,
            )
        return velocities

    def read_velocity_history(self) -> dict[str, SalesVelocity]:
        """A longer window, used only to say what a suppressed line used to
        sell. Absent history is absent, never zero: the file is optional and
        an empty result means Shannon says the history does not reach back.
        """
        path = self.fixture_dir / "velocity_history.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        window = int(data["window_days"])
        history: dict[str, SalesVelocity] = {}
        for row in data["rows"]:
            sku = str(row["sku"])
            history[sku] = SalesVelocity(
                sku=sku,
                units_sold=first_value(row["units_sold"]),
                window_days=window,
                by_channel={
                    str(channel): first_value(units)
                    for channel, units in row.get("by_channel", {}).items()
                },
            )
        return history

    def read_fba_inbound(self) -> dict[str, InboundShipment]:
        data = self._load("fba_inbound.json")
        shipments: dict[str, InboundShipment] = {}
        for row in data["shipments"]:
            sku = str(row["sku"])
            expected = row.get("expected_at")
            units = first_value(row["units"]) + (shipments[sku].units if sku in shipments else 0)
            shipments[sku] = InboundShipment(
                sku=sku,
                units=units,
                expected_at=datetime.fromisoformat(expected) if expected else None,
            )
        return shipments


def api_key(credentials_prefix: str = "") -> str:
    """The Veeqo key, from the environment and nowhere else.

    Never logged, never defaulted, never written to a report. A missing key
    stops the run with a sentence naming the variable to set.

    The name is the entity's own: `ITHRIVE_VEEQO_API_KEY`, not a shared
    one, so a second business's account can never be read with the first
    business's key.
    """
    name = f"{credentials_prefix}{VEEQO_API_KEY_VAR}"
    key = os.environ.get(name, "").strip()
    if not key:
        raise ReadFailure(
            f"{name} is not set, so Veeqo cannot be read and this week's "
            "stock is unknown. The run stops rather than treat an unreadable Veeqo as "
            "empty shelves. Put the key in the environment (or in .env) and run again."
        )
    return key


def stock_of_sellable(sku: str, entries: list[Mapping[str, Any]]) -> StockPosition:
    """One sellable's stock, split into warehouse and FBA.

    `available_stock_level` is physical minus what is already committed to
    orders, and it keeps its sign: -12 means twelve short against orders
    placed, which is a real backlog and not something to clamp.
    """
    warehouse = 0
    fba = 0
    for entry in entries:
        if entry.get("infinite"):
            # A location holding "infinite" stock is a Veeqo setting, not a
            # count. Adding a made-up number here would be worse than none.
            continue
        available = first_value(entry.get("available_stock_level", 0))
        if int(entry.get("warehouse_id", 0) or 0) == AMAZON_US_FBA_WAREHOUSE_ID:
            fba += available
        else:
            warehouse += available
    return StockPosition(sku=sku, warehouse_available=warehouse, fba_sellable=fba)


def incoming_to_fba(sku: str, entries: list[Mapping[str, Any]]) -> int:
    """Units already on their way into the Amazon warehouse.

    Veeqo carries this as `incoming_stock_level` on the FBA location's
    stock entry. It is the same fact the Phase 1 fixture called an inbound
    shipment, minus the expected-arrival date, which the API does not give.
    """
    return sum(
        first_value(entry.get("incoming_stock_level", 0))
        for entry in entries
        if int(entry.get("warehouse_id", 0) or 0) == AMAZON_US_FBA_WAREHOUSE_ID
    )


def sellables_of(product: Mapping[str, Any]) -> Iterator[tuple[str, list[Mapping[str, Any]]]]:
    """Every (SKU, stock entries) pair in one product.

    A Veeqo product is a container: a simple product has one sellable, a
    product with variants has several, and each of those is a SKU Zach
    sells. Reading only the first would lose the variants.
    """
    for sellable in product.get("sellables", []):
        sku = str(sellable.get("sku_code") or "").strip()
        if not sku:
            continue
        entries: list[Mapping[str, Any]] = [
            entry for entry in sellable.get("stock_entries", []) if isinstance(entry, dict)
        ]
        yield sku, entries


@dataclass(frozen=True)
class VeeqoLiveClient:
    """The live Veeqo account, read only.

    Every method here is a GET. There is no code in this class that could
    write to Veeqo, change an order or move stock — not disabled by a flag,
    absent.

    `channel_keys` maps the channel name Veeqo reports on an order to the
    channel key this system uses (`amazon_fba`, `amazon_fbm`, `shopify`).
    A channel Veeqo reports and configuration does not name stops the run:
    quietly dropping its sales would understate demand, and quietly adding
    them to another channel would misdirect an FBA send.

    `excluded_channels` are the ones a human has decided do not count —
    Amazon Canada and Mexico, by Zach's decision that reorder demand is US
    only. They are skipped by name and reported, never by silence: a
    channel absent from both mappings still stops the run.
    """

    channel_keys: Mapping[str, str]
    excluded_channels: frozenset[str] = frozenset()
    credentials_prefix: str = ""
    timeout_seconds: float = 30.0
    base_url: str = VEEQO_BASE_URL
    today: date | None = None
    # Injected in tests, which is how the live parsing is exercised without
    # a credential and without a network.
    transport: httpx.BaseTransport | None = field(default=None, compare=False)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={
                "x-api-key": api_key(self.credentials_prefix),
                "Accept": "application/json",
            },
            timeout=self.timeout_seconds,
            transport=self.transport,
        )

    def _pages(self, client: httpx.Client, path: str, params: dict[str, Any]) -> Iterator[Any]:
        """Every page of a paginated Veeqo collection.

        A short page ends it. A failure at any point raises: half a page of
        orders is not a smaller week, it is an unknown one.
        """
        page = 1
        while True:
            try:
                response = client.get(path, params={**params, "page": page, "page_size": PAGE_SIZE})
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise ReadFailure(
                    f"Veeqo would not answer for {path} (page {page}): {exc}. The run stops: "
                    "a partial read of Veeqo would produce a confident report built on "
                    "part of the numbers."
                ) from exc
            except ValueError as exc:
                raise ReadFailure(
                    f"Veeqo's answer for {path} (page {page}) was not readable JSON. "
                    "The run stops rather than guess at what it held."
                ) from exc
            if not isinstance(payload, list):
                raise ReadFailure(
                    f"Veeqo returned {type(payload).__name__} for {path}, not a list of "
                    "records. Nothing is assumed about the shape; the run stops."
                )
            yield from payload
            if len(payload) < PAGE_SIZE:
                return
            page += 1

    def read_inventory(self) -> dict[str, StockPosition]:
        positions: dict[str, StockPosition] = {}
        with self._client() as client:
            for product in self._pages(client, "/products", {}):
                if not isinstance(product, dict):
                    continue
                for sku, entries in sellables_of(product):
                    positions[sku] = stock_of_sellable(sku, entries)
        return positions

    def read_fba_inbound(self) -> dict[str, InboundShipment]:
        """What is already on its way to Amazon, per SKU.

        Veeqo gives the quantity but not an arrival date, so `expected_at`
        is left empty rather than filled with a plausible-looking guess.
        """
        shipments: dict[str, InboundShipment] = {}
        with self._client() as client:
            for product in self._pages(client, "/products", {}):
                if not isinstance(product, dict):
                    continue
                for sku, entries in sellables_of(product):
                    units = incoming_to_fba(sku, entries)
                    if units:
                        shipments[sku] = InboundShipment(sku=sku, units=units, expected_at=None)
        return shipments

    def read_velocity(self, window_days: int) -> dict[str, SalesVelocity]:
        end = self.today or datetime.now(tz=UTC).date()
        return self._orders_between(end - timedelta(days=window_days), end, window_days)

    def read_velocity_history(self) -> dict[str, SalesVelocity]:
        """The year before the forecast window, for suppressed lines only.

        Phase 1 invented a `velocity_history.json` export. There is no such
        export: this is the same `GET /orders` call over an earlier window,
        which is why it can answer "what did this sell before the listing
        came down" at all.
        """
        end = self.today or datetime.now(tz=UTC).date()
        window = 365
        return self._orders_between(end - timedelta(days=window), end, window)

    def _orders_between(self, start: date, end: date, window_days: int) -> dict[str, SalesVelocity]:
        totals: dict[str, int] = {}
        by_channel: dict[str, dict[str, int]] = {}
        unknown: set[str] = set()
        with self._client() as client:
            orders = self._pages(
                client,
                "/orders",
                {"created_at_min": start.isoformat(), "created_at_max": end.isoformat()},
            )
            for order in orders:
                if not isinstance(order, dict):
                    continue
                if str(order.get("status", "")).lower() in NOT_A_SALE:
                    continue
                channel_name = self._channel_name(order)
                if channel_name in self.excluded_channels:
                    # Named, decided, and printed on the report by the run;
                    # not silently dropped here.
                    continue
                channel_key = self.channel_keys.get(channel_name)
                if channel_key is None:
                    unknown.add(channel_name)
                    continue
                for item in order.get("line_items", []):
                    if not isinstance(item, dict):
                        continue
                    sellable = item.get("sellable")
                    sku = (
                        str(sellable.get("sku_code") or "").strip()
                        if isinstance(sellable, dict)
                        else ""
                    )
                    if not sku:
                        continue
                    quantity = first_value(item.get("quantity", 0))
                    totals[sku] = totals.get(sku, 0) + quantity
                    channels = by_channel.setdefault(sku, {})
                    channels[channel_key] = channels.get(channel_key, 0) + quantity
        if unknown:
            raise ReadFailure(
                "Veeqo reports sales on channels this configuration does not name: "
                + ", ".join(f"'{name}'" for name in sorted(unknown))
                + ". Shannon will not decide for herself whether those are FBA sales "
                "or merchant-fulfilled ones, because the answer moves stock to Amazon. "
                "Add each one under 'channels:' in the entity's configuration, with the "
                "name exactly as Veeqo spells it, and run again."
            )
        return {
            sku: SalesVelocity(
                sku=sku,
                units_sold=units,
                window_days=window_days,
                by_channel=dict(by_channel.get(sku, {})),
            )
            for sku, units in totals.items()
        }

    @staticmethod
    def _channel_name(order: Mapping[str, Any]) -> str:
        channel = order.get("channel")
        if isinstance(channel, dict):
            return str(channel.get("name") or channel.get("type_code") or "").strip()
        return str(channel or "").strip()


__all__ = [
    "VEEQO_API_KEY_VAR",
    "VeeqoFixtureClient",
    "VeeqoLiveClient",
    "api_key",
    "first_value",
    "incoming_to_fba",
    "sellables_of",
    "stock_of_sellable",
]
