"""Staging a supplier's cart — the first executor that touches a supplier.

Two actions, and the difference between them is the whole safety story:

* `nar.plan_cart_staging` reads the cart and writes down what would be
  added. It is Tier 0 because it changes nothing outside this machine, and
  it is the only one this phase can run.
* `nar.stage_cart` adds the lines for real. Tier 2 in the rulebook, and
  refused by the broker while `max_tier_this_phase` is 0.

Both require the `stage_cart` capability on the supplier, checked by the
broker before either is reached: a supplier Zach has not said may be
staged cannot have its cart rehearsed either.

Neither can check out. Not because a tier forbids it — because the client
underneath has no method that could, and refuses the paths and the HTTP
methods that would. See `agent_org.integrations.nar`.

What is already in the cart is read first and never touched. Zach puts
things in that cart himself; a run that tidied up after him would be a
worse failure than one that ordered nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from agent_org.broker.registry import Executor
from agent_org.config.models import Capability
from agent_org.integrations.carts import Cart, CartRefusal, CartUnavailable, SupplierCart

ACTION_PLAN_CART_STAGING = "nar.plan_cart_staging"
ACTION_STAGE_CART = "nar.stage_cart"

MODE_DRY_RUN = "DRY_RUN"
MODE_LIVE = "LIVE"

STATUS_PLANNED = "PLANNED"
STATUS_ADDED = "ADDED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"


def _cart_as_dict(cart: Cart) -> dict[str, Any]:
    return {
        "cart_id": cart.cart_id,
        "currency": cart.currency,
        "grand_total": None if cart.grand_total is None else str(cart.grand_total),
        "lines": [
            {
                "sku": line.sku,
                "name": line.name,
                "quantity": line.quantity,
                "price": None if line.price is None else str(line.price),
            }
            for line in cart.lines
        ],
    }


@dataclass
class CartStager:
    """Add this week's lines to one supplier's cart, or rehearse doing it.

    The order is fixed: read the cart, work out what has already been
    staged for this week, then act line by line, recording each one as it
    happens. Recording after the fact would mean a crash mid-run left the
    cart holding lines nothing knows about, and the retry would add them
    again.
    """

    conn: psycopg.Connection[tuple[object, ...]]
    entity_id: str
    supplier: str
    cart: SupplierCart
    dry_run: bool

    @property
    def mode(self) -> str:
        return MODE_DRY_RUN if self.dry_run else MODE_LIVE

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = str(payload["task_id"])
        slot = str(payload["schedule_slot"])
        lines = list(payload.get("lines", []))

        before = self.cart.read_cart()
        already = self._already_staged(slot)

        results: list[dict[str, Any]] = []
        for line in lines:
            results.append(self._one_line(task_id, slot, line, already, before))

        after = before if self.dry_run else self.cart.read_cart()
        if not self.dry_run:
            self._verify(results, before, after)
        return {
            "supplier": self.supplier,
            "mode": self.mode,
            "cart_before": _cart_as_dict(before),
            "cart_after": _cart_as_dict(after),
            "lines": results,
            "kept": _kept(before, after),
            "submitted": False,
            "paid": False,
            "ordered": False,
        }

    def _verify(self, results: list[dict[str, Any]], before: Cart, after: Cart) -> None:
        """Check the cart itself, not the replies that said it worked.

        Magento answering 200 for an add is Magento's account of the cart.
        This is the cart. Every line that claims to have been added has to
        show up as a real increase in what the cart holds, or it is
        reported as unverified — the confirmation email is worth nothing if
        it says "added" about a line Zach will not find.
        """
        for result in results:
            if result["status"] != STATUS_ADDED:
                continue
            sku = str(result.get("staged_sku") or result["sku"])
            expected = before.quantity_of(sku) + int(result["quantity"])
            found = after.quantity_of(sku)
            result["verified"] = found == expected
            if found != expected:
                result["detail"] = (
                    f"added as {sku} × {result['quantity']}, but the cart afterwards "
                    f"holds {found} of it where {expected} was expected. Check the "
                    "cart on the site before ordering anything."
                )

    def _one_line(
        self,
        task_id: str,
        slot: str,
        line: dict[str, Any],
        already: dict[str, str],
        before: Cart,
    ) -> dict[str, Any]:
        sku = str(line["sku"])
        quantity = int(line["quantity"])
        units = int(line.get("units", quantity))
        common = {"sku": sku, "name": str(line.get("name", "")), "quantity": quantity}

        if sku in already:
            # Idempotent by the ledger, not by hope. This is the branch a
            # crashed run comes back through.
            return {
                **common,
                "status": STATUS_SKIPPED,
                "detail": (
                    f"already staged for this week ({already[sku]}), so it was not "
                    "added a second time"
                ),
            }

        if self.dry_run:
            self._record(task_id, slot, sku, quantity, units, STATUS_PLANNED, before.cart_id, None)
            in_cart = before.quantity_of(sku)
            detail = "would be added"
            if in_cart:
                detail += f" — the cart already holds {in_cart} of this SKU, left as it is"
            return {**common, "status": STATUS_PLANNED, "detail": detail}

        try:
            staged = self.cart.add_line(sku, quantity)
        except (CartUnavailable, CartRefusal) as exc:
            self._record(
                task_id, slot, sku, quantity, units, STATUS_FAILED, before.cart_id, str(exc)
            )
            return {**common, "status": STATUS_FAILED, "detail": str(exc)}
        self._record(task_id, slot, sku, quantity, units, STATUS_ADDED, before.cart_id, None)
        return {
            **common,
            "status": STATUS_ADDED,
            # What the cart says it holds, not what we asked it to hold.
            "staged_sku": staged.sku,
            "staged_quantity": staged.quantity,
            "detail": f"added as {staged.sku} × {staged.quantity}",
        }

    def _already_staged(self, slot: str) -> dict[str, str]:
        """SKUs this week has already put in this cart, in this mode."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT sku, status FROM cart_stagings
                 WHERE entity_id = %s AND supplier = %s AND schedule_slot = %s
                   AND mode = %s AND status IN ('PLANNED', 'ADDED')
                """,
                (self.entity_id, self.supplier, slot, self.mode),
            )
            rows = cur.fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def _record(
        self,
        task_id: str,
        slot: str,
        sku: str,
        quantity: int,
        units: int,
        status: str,
        cart_id: str | None,
        error: str | None,
    ) -> None:
        """Write down what happened to this line, once and for good.

        A row that already says ADDED or PLANNED is never written over:
        that row is the record of something in a cart, and the whole point
        of the table is that it cannot be undone or forgotten. A row that
        says FAILED is a different matter — the attempt after it is the one
        that put the line in the cart, and leaving the old status there
        would hide a real cart line from the next run's skip.
        """
        self.conn.execute(
            """
            INSERT INTO cart_stagings (entity_id, task_id, supplier, schedule_slot, sku,
                                       quantity, units, mode, status, cart_id, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (entity_id, supplier, schedule_slot, sku, mode) DO UPDATE
               SET task_id = EXCLUDED.task_id,
                   quantity = EXCLUDED.quantity,
                   units = EXCLUDED.units,
                   status = EXCLUDED.status,
                   cart_id = EXCLUDED.cart_id,
                   error = EXCLUDED.error,
                   updated_at = clock_timestamp()
             WHERE cart_stagings.status NOT IN ('PLANNED', 'ADDED')
            """,
            (
                self.entity_id,
                task_id,
                self.supplier,
                slot,
                sku,
                quantity,
                units,
                self.mode,
                status,
                cart_id,
                error,
            ),
        )


def _kept(before: Cart, after: Cart) -> dict[str, Any]:
    """Did everything Zach already had in the cart survive the run?

    Shannon cannot remove a line — there is no method for it — so this can
    only fail if the site did something of its own. It is reported anyway,
    because "your cart is as you left it, plus these" is the sentence the
    confirmation email is really making.
    """
    lost = [
        {"sku": line.sku, "was": line.quantity, "now": after.quantity_of(line.sku)}
        for line in before.lines
        if after.quantity_of(line.sku) < line.quantity
    ]
    return {"all_kept": not lost, "lost": lost}


def plan_cart_staging_executor(stager: CartStager) -> Executor:
    """The rehearsal. Reads the supplier's cart; changes nothing there.

    Internal and reversible, because the only thing it produces is a list
    on this machine — which is why it is allowed while the phase ceiling is
    0, and why proving the plan costs nothing.
    """
    return Executor(
        action_type=ACTION_PLAN_CART_STAGING,
        reversible="yes",
        category="internal",
        supplier=stager.supplier,
        requires_capability=Capability.STAGE_CART,
        run=stager,
    )


def stage_cart_executor(stager: CartStager) -> Executor:
    """The real thing. Adds lines to a supplier's cart, and never buys them.

    Category `purchase` although no money moves: it is the category the
    escalations are written against, and a staging run with no trailing
    history to compare against escalates to Tier 3 by that route. That is
    the intended answer for the first live staging — it should need Zach,
    twice.
    """
    return Executor(
        action_type=ACTION_STAGE_CART,
        # A cart line can be taken out by hand, but not by Shannon: she has
        # no capability to remove one. 'window' is the honest word for that.
        reversible="window",
        category="purchase",
        supplier=stager.supplier,
        requires_capability=Capability.STAGE_CART,
        run=stager,
    )


__all__ = [
    "ACTION_PLAN_CART_STAGING",
    "ACTION_STAGE_CART",
    "MODE_DRY_RUN",
    "MODE_LIVE",
    "STATUS_ADDED",
    "STATUS_FAILED",
    "STATUS_PLANNED",
    "STATUS_SKIPPED",
    "CartStager",
    "plan_cart_staging_executor",
    "stage_cart_executor",
]
