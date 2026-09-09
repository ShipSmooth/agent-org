"""What a supplier cart looks like from Shannon's side, and nothing more.

One shape for every supplier, so the executor that stages a cart does not
care whether the site behind it is Magento, a portal form or an API. The
implementations live beside this file; the rule they all obey lives here:

* a cart can be read,
* a line can be added to it,
* and there is no method on this interface for checking out, paying,
  placing an order or emptying a cart.

That last point is the permanent constraint written as code rather than as
configuration. A tier can be raised in a YAML file; a method that does not
exist cannot be called by raising anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol


class CartUnavailable(RuntimeError):
    """The cart could not be read or written, and nothing was assumed.

    Same discipline as a failed Veeqo read: an unreadable cart is unknown,
    never empty. A confirmation report that said "your cart is empty"
    because the site was down would be worse than no report.
    """


class CartRefusal(RuntimeError):
    """Something asked the cart to do what it must never do.

    Raised for a path outside the allow-list, for a request method that
    could place an order, and for a live add when only a dry run was
    authorised. It is a bug in the caller, not a supplier failure.
    """


@dataclass(frozen=True)
class CartLine:
    """One line already in the supplier's cart."""

    sku: str
    name: str
    quantity: int
    price: Decimal | None = None
    item_id: str | None = None


@dataclass(frozen=True)
class Cart:
    """A supplier cart as it stands, before Shannon touches it."""

    supplier: str
    cart_id: str
    lines: tuple[CartLine, ...]
    grand_total: Decimal | None = None
    currency: str = "USD"

    def quantity_of(self, sku: str) -> int:
        return sum(line.quantity for line in self.lines if line.sku == sku)


class SupplierCart(Protocol):
    """Read a cart; add one line to it. Deliberately nothing else."""

    supplier: str

    def read_cart(self) -> Cart: ...

    def add_line(self, sku: str, quantity: int) -> CartLine: ...


def money(value: Any) -> Decimal | None:
    """A price, or nothing. Never a guess and never a zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class SavedCartCopy:
    """A supplier's cart as a saved JSON file, for a dry run with no account.

    Every supplier's dry run reads one of these, and none of them can add
    a line: `add_line` exists only to refuse, because a saved cart has
    nothing to add to. That refusal is also what a live run pointed at a
    saved copy trips over, which is the bug this shape stops repeating.
    """

    fixture_dir: Path
    supplier: str = ""
    filename: str = ""

    def read_cart(self) -> Cart:
        path = self.fixture_dir / self.filename
        if not path.exists():
            raise CartUnavailable(
                f"The saved cart '{path}' is missing, so what is already in the "
                "cart is unknown. Nothing is reported as staged; an unreadable "
                "cart is not an empty one."
            )
        body = json.loads(path.read_text(encoding="utf-8-sig"))
        lines = tuple(
            CartLine(
                sku=str(item["sku"]),
                name=str(item.get("name", "")),
                quantity=int(item.get("qty", 0)),
                price=money(item.get("price")),
            )
            for item in body.get("items", [])
        )
        return Cart(
            supplier=self.supplier,
            cart_id=str(body.get("id", "fixture")),
            lines=lines,
            grand_total=money(body.get("grand_total")),
        )

    def add_line(self, sku: str, quantity: int) -> CartLine:
        raise CartRefusal(
            f"This is a saved copy of the cart, not the cart. {quantity} of {sku} "
            "was not added anywhere."
        )


__all__ = [
    "Cart",
    "CartLine",
    "CartRefusal",
    "CartUnavailable",
    "SavedCartCopy",
    "SupplierCart",
    "money",
]
