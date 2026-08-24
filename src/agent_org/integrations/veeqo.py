"""Veeqo read client — stock, 90-day velocity, FBA inbound. Read only.

Operational facts honoured (docs/replenishment.md §11, the NAR reorder
procedure):

- Springfield warehouse = "Warehouse (7701)", id 70459; Amazon US FBA =
  id 192025.
- on_hand = available at every ordinary warehouse (negative available is
  a real backlog — the sign is kept, never clamped) plus FBA *sellable*
  only. FBA reserved and unfulfillable stock never count.
- Velocity comes from the products report with a 90-day window. Each
  numeric cell may show two values — current period first, comparison
  second ("540 / 480"); the FIRST is used.
- Shopify inventory numbers are placeholders and are never read here.

Phase 1 is built and tested against fixture files shaped like the data
above (a directory of JSON files); no live account is contacted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

SPRINGFIELD_WAREHOUSE_ID = 70459
FBA_WAREHOUSE_ID = 192025
# The only two locations holding real stock. Everything else Veeqo reports
# (notably the Shopify store's placeholder quantities) is ignored.
STOCK_LOCATION_IDS = frozenset({SPRINGFIELD_WAREHOUSE_ID, FBA_WAREHOUSE_ID})


class VeeqoReadError(RuntimeError):
    """A Veeqo read failed or made no sense. The run must stop, not guess."""


def first_value(cell: object) -> int:
    """A report cell may be '540 / 480' (current / comparison): use the first."""
    if isinstance(cell, int):
        return cell
    if isinstance(cell, str):
        match = re.match(r"^\s*(-?[\d,]*\d)", cell)
        if match:
            return int(match.group(1).replace(",", ""))
    raise VeeqoReadError(f"Could not read a number from the report cell {cell!r}.")


@dataclass(frozen=True)
class StockLevel:
    sku: str
    warehouse_available: int  # every ordinary warehouse; may be negative
    fba_sellable: int  # sellable only — never reserved/unfulfillable

    @property
    def on_hand(self) -> int:
        return self.warehouse_available + self.fba_sellable


@dataclass
class VeeqoSnapshot:
    stock: dict[str, StockLevel]
    velocity_units: dict[str, dict[str, int]]  # sku -> channel key -> units in window
    window_days: int
    fba_inbound: dict[str, int]  # sku -> units inbound
    ignored_locations: set[int] = field(default_factory=set)

    def weekly_velocity(self, sku: str, channel: str) -> Fraction:
        units = self.velocity_units.get(sku, {}).get(channel, 0)
        return Fraction(units * 7, self.window_days)

    def total_weekly_velocity(self, sku: str) -> Fraction:
        return sum(
            (self.weekly_velocity(sku, ch) for ch in self.velocity_units.get(sku, {})),
            Fraction(0),
        )

    def sold_skus(self) -> set[str]:
        return {sku for sku, per_ch in self.velocity_units.items() if any(per_ch.values())}


class VeeqoReadClient:
    """Reads Veeqo-shaped data from a fixture directory.

    Expected files: ``veeqo_stock.json``, ``veeqo_products_report.json``,
    ``veeqo_fba_inbound.json``. The parsing here is exactly what a live
    transport would feed; only the source of bytes changes later.
    """

    def __init__(self, fixtures_dir: Path) -> None:
        self._dir = fixtures_dir

    def _load(self, name: str) -> object:
        path = self._dir / name
        if not path.exists():
            raise VeeqoReadError(
                f"Veeqo data file {path} is missing. The run stops rather than guess."
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise VeeqoReadError(f"Veeqo data file {path} is not valid JSON: {exc}.") from exc

    def snapshot(self) -> VeeqoSnapshot:
        velocity_units, window_days = self._read_report()
        stock, ignored = self._read_stock()
        return VeeqoSnapshot(
            stock=stock,
            velocity_units=velocity_units,
            window_days=window_days,
            fba_inbound=self._read_inbound(),
            ignored_locations=ignored,
        )

    def _read_stock(self) -> tuple[dict[str, StockLevel], set[int]]:
        doc = self._load("veeqo_stock.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("products"), list):
            raise VeeqoReadError("veeqo_stock.json should contain a 'products' list.")
        out: dict[str, StockLevel] = {}
        ignored: set[int] = set()
        for product in doc["products"]:
            if not isinstance(product, dict):
                continue
            sku = product.get("sku")
            entries = product.get("stock_entries")
            if not isinstance(sku, str) or not isinstance(entries, list):
                raise VeeqoReadError(
                    "Every product in veeqo_stock.json needs 'sku' and 'stock_entries'."
                )
            warehouse_available = 0
            fba_sellable = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                warehouse_id = entry.get("warehouse_id")
                if isinstance(warehouse_id, int) and warehouse_id not in STOCK_LOCATION_IDS:
                    # Any other Veeqo location (the Shopify store's placeholder
                    # quantities among them) is never stock.
                    ignored.add(warehouse_id)
                    continue
                if warehouse_id == FBA_WAREHOUSE_ID:
                    sellable = entry.get("sellable")
                    if not isinstance(sellable, int):
                        raise VeeqoReadError(
                            f"FBA stock for {sku} has no integer 'sellable' figure — "
                            "reserved and unfulfillable stock must never be counted, "
                            "so the run stops rather than substitute another number."
                        )
                    fba_sellable += sellable
                else:
                    available = entry.get("available")
                    if not isinstance(available, int):
                        raise VeeqoReadError(
                            f"Warehouse stock for {sku} has no integer 'available' figure."
                        )
                    # negative available is a real backlog: keep the sign.
                    warehouse_available += available
            previous = out.get(sku)
            if previous is not None:
                warehouse_available += previous.warehouse_available
                fba_sellable += previous.fba_sellable
            out[sku] = StockLevel(sku, warehouse_available, fba_sellable)
        return out, ignored

    def _read_report(self) -> tuple[dict[str, dict[str, int]], int]:
        doc = self._load("veeqo_products_report.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
            raise VeeqoReadError("veeqo_products_report.json should contain a 'rows' list.")
        window = doc.get("window_days")
        if not isinstance(window, int) or window <= 0:
            raise VeeqoReadError("veeqo_products_report.json needs a positive 'window_days'.")
        out: dict[str, dict[str, int]] = {}
        for row in doc["rows"]:
            if not isinstance(row, dict):
                continue
            sku = row.get("sku")
            per_channel = row.get("units_sold_by_channel")
            if not isinstance(sku, str) or not isinstance(per_channel, dict):
                raise VeeqoReadError("Every report row needs 'sku' and 'units_sold_by_channel'.")
            out[sku] = {str(ch): first_value(cell) for ch, cell in per_channel.items()}
        return out, window

    def _read_inbound(self) -> dict[str, int]:
        doc = self._load("veeqo_fba_inbound.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("shipments"), list):
            raise VeeqoReadError("veeqo_fba_inbound.json should contain a 'shipments' list.")
        out: dict[str, int] = {}
        for shipment in doc["shipments"]:
            if not isinstance(shipment, dict):
                continue
            for line in shipment.get("lines", []):
                if not isinstance(line, dict):
                    continue
                sku = line.get("sku")
                qty = line.get("quantity")
                if not isinstance(sku, str) or not isinstance(qty, int):
                    raise VeeqoReadError("Every inbound line needs 'sku' and 'quantity'.")
                out[sku] = out.get(sku, 0) + qty
        return out
