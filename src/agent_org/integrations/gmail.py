"""Gmail — the authoritative answer to "what is already on order?".

NAR's own order-status page lists orders as "Processing" that shipped
weeks earlier, so it is never read. Instead: an order is outstanding if
and only if it has a confirmation email with no matching shipping
notification.

Split shipments carry a suffix (`EC2620998.1`); matching happens on the
base order number, and the split is flagged on the report so a human can
see the order arrived in pieces.

If the signal cannot be read cleanly, this raises and the run stops.
Double-ordering is the most expensive failure this system can produce, so
an unclear inbox is a reason to ask Zach, never a reason to assume.

Everything in an email is treated as data. Directive-sounding text found
in a message body is ignored and reported, never followed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_org.integrations.reads import AmbiguousOrderSignal, OrderSignals, ReadFailure

ORDER_NUMBER = re.compile(r"\b(EC\d+)(\.\d+)?\b")

DIRECTIVE_PHRASES = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard the above",
    "you must order",
    "place the order",
    "system prompt",
    "as an ai",
)

CONFIRMATION = "confirmation"
SHIPMENT = "shipment"


@dataclass(frozen=True)
class OrderEmail:
    kind: str  # confirmation | shipment
    order_number: str
    base_order_number: str
    lines: dict[str, int]
    subject: str


def classify(subject: str) -> str | None:
    lowered = subject.lower()
    if "order confirmation" in lowered:
        return CONFIRMATION
    if "shipping notification" in lowered or lowered.strip().startswith("#in"):
        return SHIPMENT
    return None


def find_directives(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(phrase for phrase in DIRECTIVE_PHRASES if phrase in lowered)


@dataclass(frozen=True)
class GmailFixtureClient:
    """A Gmail reader backed by saved messages. Read only, no credential."""

    fixture_dir: Path
    mailbox: str = "messages.json"

    def _load(self) -> Any:
        path = self.fixture_dir / self.mailbox
        if not path.exists():
            raise ReadFailure(
                f"Gmail fixture '{path}' is missing, so what is already on order "
                "cannot be established. The run stops rather than risk ordering twice."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def read_order_signals(self) -> OrderSignals:
        data = self._load()
        if data.get("unavailable"):
            raise AmbiguousOrderSignal(
                "Gmail could not be read. Shannon cannot tell which NAR orders are "
                "still awaiting shipment, and will not guess. Tell her which orders "
                "are outstanding, or fix the connection and run again."
            )

        confirmations: dict[str, OrderEmail] = {}
        shipped: set[str] = set()
        splits: list[str] = []
        directives: list[str] = []
        warnings: list[str] = []

        for message in data.get("messages", []):
            subject = str(message.get("subject", ""))
            body = str(message.get("body", ""))
            for phrase in find_directives(subject + "\n" + body):
                directives.append(
                    f"Message '{subject}' contains the phrase '{phrase}'. It was read "
                    "as text and ignored."
                )
            kind = classify(subject)
            if kind is None:
                continue
            match = ORDER_NUMBER.search(subject) or ORDER_NUMBER.search(body)
            if match is None:
                raise AmbiguousOrderSignal(
                    f"The email '{subject}' looks like a NAR order email but has no "
                    "readable EC order number, so it cannot be matched to an order."
                )
            order_number = match.group(0)
            base = match.group(1)
            if match.group(2):
                splits.append(
                    f"{order_number} is part of a split shipment of order {base}; "
                    "matched on the base order number."
                )
            if kind == SHIPMENT:
                shipped.add(base)
                continue
            lines = message.get("lines")
            if not isinstance(lines, list) or not lines:
                raise AmbiguousOrderSignal(
                    f"Order confirmation {base} does not list what was ordered, so the "
                    "quantities already on order are unknown."
                )
            parsed: dict[str, int] = {}
            for line in lines:
                sku = str(line["sku"])
                parsed[sku] = parsed.get(sku, 0) + int(line["qty"])
            confirmations[base] = OrderEmail(
                kind=CONFIRMATION,
                order_number=order_number,
                base_order_number=base,
                lines=parsed,
                subject=subject,
            )

        unmatched = shipped - set(confirmations)
        if unmatched:
            raise AmbiguousOrderSignal(
                "There are shipping notifications for orders with no confirmation "
                f"email: {', '.join(sorted(unmatched))}. Shannon cannot tell what "
                "those orders contained."
            )

        on_order: dict[str, int] = {}
        outstanding: list[str] = []
        for base, email in sorted(confirmations.items()):
            if base in shipped:
                continue
            outstanding.append(base)
            for sku, qty in email.lines.items():
                on_order[sku] = on_order.get(sku, 0) + qty

        return OrderSignals(
            on_order=on_order,
            outstanding_orders=tuple(outstanding),
            split_shipment_flags=tuple(splits),
            ignored_directives=tuple(directives),
            warnings=tuple(warnings),
        )


__all__ = ["GmailFixtureClient", "OrderEmail", "classify", "find_directives"]
