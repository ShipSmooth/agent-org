"""Gmail read client — the authoritative `on_order` source. Read only.

docs/replenishment.md §3.1: an order is outstanding if and only if it has
a confirmation email with no matching shipping notification. Order
numbers are `EC…`; split shipments carry a suffix (`EC2620998.1`) —
matching is on the BASE order number, and a seen suffix is flagged in the
report (a partially-shipped order drops its remainder from `on_order`,
conservative in the right direction, but worth telling Zach).

If the mailbox is unavailable or a message is ambiguous, the run STOPS
and says so. Double-ordering is the most expensive failure mode in this
system; Shannon never guesses.

Phase 1 reads a fixture mailbox (``gmail_messages.json``); no live
account is contacted. Everything read from email is data, never
instructions — nothing in a message body is ever executed or obeyed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CONFIRMATION_SUBJECT = "Your North American Rescue, LLC order confirmation"
SHIPPING_SUBJECT_PREFIX = "Shipping Notification Order: "
NAR_SENDER = "info@narescue.com"

_ORDER_RE = re.compile(r"\b(EC\d+)(\.(\d+))?\b")
_LINE_RE = re.compile(r"^\s*([0-9A-Za-z-]+)\s+x\s+(\d+)\s*$")


class GmailReadError(RuntimeError):
    """Gmail unavailable or a signal is ambiguous. Stop and ask; never guess."""


@dataclass(frozen=True)
class OutstandingOrder:
    order_number: str  # base EC number
    lines: dict[str, int]  # part number -> quantity ordered


@dataclass
class OnOrderResult:
    outstanding: list[OutstandingOrder]
    split_shipment_flags: list[str] = field(default_factory=list)

    def units_on_order(self, part: str) -> int:
        return sum(order.lines.get(part, 0) for order in self.outstanding)


def _base_order_number(text: str, *, where: str) -> tuple[str, bool]:
    match = _ORDER_RE.search(text)
    if match is None:
        raise GmailReadError(
            f"Could not find an EC order number in {where}. The run stops here: "
            "Zach needs to say which orders are still awaiting shipment."
        )
    return match.group(1), match.group(2) is not None


def _parse_lines(body: str, order_number: str) -> dict[str, int]:
    lines: dict[str, int] = {}
    in_items = False
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.lower() == "items:":
            in_items = True
            continue
        if not in_items:
            continue
        if not stripped:
            break
        match = _LINE_RE.match(stripped)
        if match is None:
            raise GmailReadError(
                f"The confirmation for order {order_number} has a line that could "
                f"not be read ({stripped!r}). The run stops here rather than "
                "guess what is on order."
            )
        part, qty = match.group(1), int(match.group(2))
        lines[part] = lines.get(part, 0) + qty
    if not lines:
        raise GmailReadError(
            f"The confirmation for order {order_number} lists no items that could "
            "be read. The run stops here rather than guess what is on order."
        )
    return lines


class GmailReadClient:
    """Reads a fixture mailbox: a JSON list of {subject, from, body}."""

    def __init__(self, fixtures_dir: Path) -> None:
        self._path = fixtures_dir / "gmail_messages.json"

    def on_order(self) -> OnOrderResult:
        if not self._path.exists():
            raise GmailReadError(
                f"The mailbox data file {self._path} is missing. Gmail is the "
                "authoritative source for outstanding orders; without it the run "
                "stops — exactly like a Veeqo failure."
            )
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GmailReadError(f"The mailbox data file is not valid JSON: {exc}.") from exc
        if not isinstance(doc, list):
            raise GmailReadError("The mailbox data file should be a list of messages.")

        confirmations: dict[str, OutstandingOrder] = {}
        shipped: set[str] = set()
        flags: list[str] = []
        for message in doc:
            if not isinstance(message, dict):
                raise GmailReadError("Every mailbox entry should be a message mapping.")
            subject = str(message.get("subject", ""))
            sender = str(message.get("from", ""))
            body = str(message.get("body", ""))
            if sender != NAR_SENDER and not subject.startswith("#IN"):
                continue
            if subject == CONFIRMATION_SUBJECT:
                base, _ = _base_order_number(body, where="a confirmation email")
                order = OutstandingOrder(base, _parse_lines(body, base))
                existing = confirmations.get(base)
                if existing is not None and existing.lines != order.lines:
                    raise GmailReadError(
                        f"Order {base} has two different confirmation emails "
                        f"({existing.lines} and {order.lines}). Shannon cannot tell "
                        "which is current, so the run stops and asks rather than "
                        "guessing what is on order."
                    )
                confirmations[base] = order
            elif subject.startswith(SHIPPING_SUBJECT_PREFIX):
                base, has_suffix = _base_order_number(
                    subject, where=f"the shipping notification {subject!r}"
                )
                shipped.add(base)
                if has_suffix:
                    flags.append(
                        f"Order {base} shipped with a split-shipment suffix — the whole "
                        "order is treated as shipped, so any unshipped remainder is NOT "
                        "counted as on order. Worth checking."
                    )
            elif subject.startswith("#IN"):
                # paid-invoice email from a NAR rep: also confirms shipment.
                base, _ = _base_order_number(body, where=f"the invoice email {subject!r}")
                shipped.add(base)

        outstanding = [
            order for base, order in sorted(confirmations.items()) if base not in shipped
        ]
        return OnOrderResult(outstanding=outstanding, split_shipment_flags=flags)
