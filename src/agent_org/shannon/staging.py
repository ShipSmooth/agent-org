"""What Shannon would put in a supplier's cart, worked out from what she
already reported.

No arithmetic happens here. The week's report is the decision; this module
reads its lines back and turns the ones routed to a supplier's cart into
the exact SKUs and quantities that would be added. If the plan and the
report ever disagree, the plan is wrong, and that is the point of building
it from the report row rather than from a fresh calculation.

Two conversions, both of which have been got wrong by hand before:

* The quantity added is `purchase_units`, never `order_units`. A component
  bought in boxes of 25 needs 4 boxes, not 100 boxes.
* A line whose part number is ours, because the supplier publishes none,
  cannot be added by SKU at all. It is left out with the reason said out
  loud rather than staged under an invented number.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_org.config.models import ComponentKey

CART_SUFFIX = "_cart"


@dataclass(frozen=True)
class StagingLine:
    """One line that would be added to a supplier's cart."""

    key: ComponentKey
    name: str
    sku: str
    quantity: int
    units: int
    purchase_unit_name: str | None
    units_per_purchase_unit: int | None

    @property
    def how_much(self) -> str:
        """The quantity in words, so a box is never read as a unit."""
        if self.units_per_purchase_unit and self.purchase_unit_name:
            return (
                f"{self.quantity} × {self.purchase_unit_name} "
                f"({self.units_per_purchase_unit} each = {self.units} units)"
            )
        return f"{self.quantity} units"


@dataclass(frozen=True)
class SkippedLine:
    """A line the report wants ordered that cannot be staged, and why."""

    key: ComponentKey
    name: str
    reason: str


@dataclass(frozen=True)
class StagingPlan:
    supplier: str
    lines: tuple[StagingLine, ...]
    skipped: tuple[SkippedLine, ...]

    @property
    def routing(self) -> str:
        return f"{self.supplier}{CART_SUFFIX}"

    def __bool__(self) -> bool:
        return bool(self.lines)


def _key_of(line: Mapping[str, Any]) -> ComponentKey:
    supplier, _, part = str(line.get("component", "")).partition("/")
    return ComponentKey(supplier=supplier, part=part)


def plan_from_report_lines(
    lines: Iterable[Mapping[str, Any]],
    supplier: str,
) -> StagingPlan:
    """Turn the report's own rows into a cart plan for one supplier.

    `lines` is the `lines` column of the report row — the numbers exactly
    as they were reported and emailed, not a recomputation of them.
    """
    routing = f"{supplier}{CART_SUFFIX}"
    staged: list[StagingLine] = []
    skipped: list[SkippedLine] = []
    for line in lines:
        if str(line.get("routing")) != routing:
            continue
        key = _key_of(line)
        name = str(line.get("name", ""))
        units = int(line.get("rounded_to_five") or 0)
        purchase_units = line.get("purchase_units")
        quantity = int(purchase_units) if purchase_units else units
        if units <= 0 or quantity <= 0:
            # Routed to the cart with nothing to order is a contradiction,
            # so it is reported rather than silently dropped.
            skipped.append(
                SkippedLine(
                    key=key,
                    name=name,
                    reason="the report routes this to the cart but orders none of it",
                )
            )
            continue
        if bool(line.get("part_is_internal_reference")):
            skipped.append(
                SkippedLine(
                    key=key,
                    name=name,
                    reason=(
                        f"'{key.part}' is our own reference, not a {supplier} part "
                        "number, so there is no SKU to add. Order this one by name."
                    ),
                )
            )
            continue
        staged.append(
            StagingLine(
                key=key,
                name=name,
                sku=key.part,
                quantity=quantity,
                units=units,
                purchase_unit_name=(
                    str(line["purchase_unit_name"]) if line.get("purchase_unit_name") else None
                ),
                units_per_purchase_unit=(
                    int(line["units_per_purchase_unit"])
                    if line.get("units_per_purchase_unit")
                    else None
                ),
            )
        )
    return StagingPlan(supplier=supplier, lines=tuple(staged), skipped=tuple(skipped))


__all__ = ["CART_SUFFIX", "SkippedLine", "StagingLine", "StagingPlan", "plan_from_report_lines"]
