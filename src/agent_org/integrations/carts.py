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

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


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


__all__ = [
    "Cart",
    "CartLine",
    "CartRefusal",
    "CartUnavailable",
    "SupplierCart",
]
