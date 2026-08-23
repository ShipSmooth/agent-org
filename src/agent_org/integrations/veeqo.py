"""Veeqo — read only, and in this phase from fixtures on disk.

Two facts from the reorder procedure are encoded here rather than left to
whoever reads the report:

* The products report prints two numbers in one cell — the current window
  and the comparison window, e.g. "450 (390)". The first is the one that
  means anything for this week. `first_value` takes it.
* Negative availability is real. A SKU at -12 is twelve units short
  against orders already placed; clamping that to zero would quietly
  under-order by twelve.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_org.integrations.reads import (
    AMAZON_US_FBA_WAREHOUSE_ID,
    InboundShipment,
    ReadFailure,
    SalesVelocity,
    StockPosition,
)

NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


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
        return json.loads(path.read_text(encoding="utf-8"))

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


__all__ = ["VeeqoFixtureClient", "first_value"]
