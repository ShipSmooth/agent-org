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
in a message body is ignored and reported, never followed. That holds for
the live mailbox exactly as it held for the fixtures, because the live
reader hands the same message shape to the same parser: the defence is one
implementation, not two that can drift.

This module is read-only twice over — the scope requested is Gmail's
read-only scope, and there is no send, reply, forward, label or delete call
in it. The single message Shannon sends is the weekly report, and it leaves
through SMTP in notify/email.py to a role resolved from configuration.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from agent_org.integrations.reads import AmbiguousOrderSignal, OrderSignals, ReadFailure

ORDER_NUMBER = re.compile(r"\b(EC\d+)(\.\d+)?\b")

# A confirmation lists each line as a part number and a quantity on one line
# of text. Anything else is not guessed at: a confirmation whose lines
# cannot be read raises, because "no lines found" and "nothing on order"
# are different facts and treating one as the other is how NAR gets paid
# twice for the same tourniquets.
CONFIRMATION_LINE = re.compile(
    r"^\s*(?P<sku>[A-Z0-9]{2}-[A-Z0-9-]{3,})\b.*?(?P<qty>\d{1,6})\s*(?:ea|each|units?|pcs?)?\s*$",
    re.IGNORECASE,
)

BLOCK_TAG = re.compile(r"</?(?:br|p|div|tr|td|th|li|table|h[1-6])\b[^>]*>", re.IGNORECASE)

GMAIL_CLIENT_ID_VAR = "GMAIL_CLIENT_ID"
GMAIL_CLIENT_SECRET_VAR = "GMAIL_CLIENT_SECRET"
GMAIL_REFRESH_TOKEN_VAR = "GMAIL_REFRESH_TOKEN"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_API_BASE = "https://gmail.googleapis.com"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# NAR order mail from the last two months, which is the window Zach's own
# procedure uses. A three-week lead time means anything older than that with
# no shipping notice is a question for him, not a quantity to add in.
DEFAULT_QUERY = "from:narescue.com newer_than:60d"
MAX_MESSAGES = 500

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


def _describe(lines: dict[str, int]) -> str:
    return ", ".join(f"{sku} × {qty}" for sku, qty in sorted(lines.items()))


def find_directives(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(phrase for phrase in DIRECTIVE_PHRASES if phrase in lowered)


def lines_from_body(body: str) -> dict[str, int]:
    """The part numbers and quantities a confirmation email lists.

    Only a line that reads as a part number followed by a quantity counts.
    A subtotal, an address or a marketing footer does not, and a line that
    cannot be read is left out rather than approximated — the caller then
    sees a confirmation with no lines and stops the run, which is the safe
    direction.
    """
    found: dict[str, int] = {}
    for raw in body.splitlines():
        match = CONFIRMATION_LINE.match(raw)
        if match is None:
            continue
        quantity = int(match.group("qty"))
        if quantity <= 0:
            continue
        sku = match.group("sku").strip()
        found[sku] = found.get(sku, 0) + quantity
    return found


def order_signals_from(messages: Sequence[Mapping[str, Any]]) -> OrderSignals:
    """Work out what is still on order from a set of messages.

    Saved fixtures and the live mailbox both arrive here as `{subject, body,
    lines}`, so the prompt-injection defence and the confirmation-versus-
    shipping reconciliation are the same code against saved data and against
    Zach's real inbox.
    """
    confirmations: dict[str, OrderEmail] = {}
    shipped: set[str] = set()
    splits: list[str] = []
    directives: list[str] = []
    warnings: list[str] = []

    for message in messages:
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
        parsed: dict[str, int] = {}
        if isinstance(lines, list) and lines:
            for line in lines:
                sku = str(line["sku"])
                parsed[sku] = parsed.get(sku, 0) + int(line["qty"])
        else:
            parsed = lines_from_body(body)
        if not parsed:
            raise AmbiguousOrderSignal(
                f"Order confirmation {base} does not list what was ordered in a form "
                "Shannon can read, so the quantities already on order are unknown. "
                "She stops rather than treat that order as nothing: ordering the same "
                "lines twice is the most expensive mistake this system can make. Tell "
                f"her what {base} contained, or open its detail page on narescue.com."
            )
        existing = confirmations.get(base)
        if existing is not None and existing.lines != parsed:
            raise AmbiguousOrderSignal(
                f"Order {base} has two confirmation emails that disagree about "
                f"what was ordered ({_describe(existing.lines)} against "
                f"{_describe(parsed)}). Shannon cannot tell which is current, so "
                "the run stops rather than guess what is already on order."
            )
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
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def read_order_signals(self) -> OrderSignals:
        data = self._load()
        if data.get("unavailable"):
            raise AmbiguousOrderSignal(
                "Gmail could not be read. Shannon cannot tell which NAR orders are "
                "still awaiting shipment, and will not guess. Tell her which orders "
                "are outstanding, or fix the connection and run again."
            )
        return order_signals_from(list(data.get("messages", [])))


def oauth_settings(credentials_prefix: str = "") -> tuple[str, str, str]:
    """The Gmail OAuth values, from the environment and nowhere else.

    A refresh token, not a password: it carries only the read-only scope, so
    even a stolen one cannot send mail as Zach. None of the three is ever
    logged or written to a report.

    The names carry the entity's prefix — `ITHRIVE_GMAIL_CLIENT_ID` — so
    one business's mailbox cannot be read on another's behalf.
    """
    names = tuple(
        f"{credentials_prefix}{name}"
        for name in (GMAIL_CLIENT_ID_VAR, GMAIL_CLIENT_SECRET_VAR, GMAIL_REFRESH_TOKEN_VAR)
    )
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise ReadFailure(
            "Gmail cannot be read because "
            + ", ".join(missing)
            + " is not set in the environment, so what is already on order is "
            "unknown. The run stops rather than risk ordering the same lines twice."
        )
    identifier, secret, refresh = names
    return (
        os.environ[identifier].strip(),
        os.environ[secret].strip(),
        os.environ[refresh].strip(),
    )


def decode_body(payload: Mapping[str, Any]) -> str:
    """The plain text of a Gmail message.

    Gmail nests a message's parts and base64url-encodes each one. The text
    part is preferred; HTML is taken only when there is no text part, and
    then its tags are stripped, because a directive hidden inside markup is
    still text that has to be seen in order to be ignored and reported.
    """
    text: list[str] = []
    html: list[str] = []

    def walk(part: Mapping[str, Any]) -> None:
        mime = str(part.get("mimeType", ""))
        data = part.get("body", {})
        encoded = data.get("data") if isinstance(data, dict) else None
        if isinstance(encoded, str) and encoded:
            try:
                decoded = base64.urlsafe_b64decode(encoded + "===").decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                decoded = ""
            if mime == "text/plain":
                text.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for child in part.get("parts", []) or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    if text:
        return "\n".join(text)
    # Each block and each table cell becomes its own line first: an order
    # line lives in a table row, and flattening the markup to one long
    # string would leave nothing that reads as "part number, quantity".
    markup = re.sub(BLOCK_TAG, "\n", "\n".join(html))
    return re.sub(r"<[^>]+>", " ", markup)


def header(headers: Sequence[Mapping[str, Any]], name: str) -> str:
    for entry in headers:
        if str(entry.get("name", "")).lower() == name.lower():
            return str(entry.get("value", ""))
    return ""


@dataclass(frozen=True)
class GmailLiveClient:
    """Zach's real mailbox, read only.

    Reads order confirmations and shipping notices and nothing else. There
    is no code here that sends, replies, forwards, labels, archives or
    deletes: the OAuth scope requested would refuse those calls, and the
    calls are not written in the first place.
    """

    credentials_prefix: str = ""
    query: str = DEFAULT_QUERY
    user_id: str = "me"
    timeout_seconds: float = 30.0
    api_base: str = GMAIL_API_BASE
    token_url: str = GOOGLE_TOKEN_URL
    # Injected in tests, which is how live parsing is exercised without a
    # credential and without a network.
    transport: httpx.BaseTransport | None = field(default=None, compare=False)

    def _access_token(self, client: httpx.Client) -> str:
        client_id, client_secret, refresh_token = oauth_settings(self.credentials_prefix)
        try:
            response = client.post(
                self.token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": GMAIL_READONLY_SCOPE,
                },
            )
            response.raise_for_status()
            token = str(response.json().get("access_token", ""))
        except (httpx.HTTPError, ValueError) as exc:
            raise AmbiguousOrderSignal(
                "Google would not renew Shannon's read-only Gmail access, so what is "
                f"already on order cannot be established ({exc}). She stops rather "
                "than assume nothing is outstanding."
            ) from exc
        if not token:
            raise AmbiguousOrderSignal(
                "Google renewed nothing usable for Gmail access, so what is already "
                "on order cannot be established. The run stops."
            )
        return token

    def read_order_signals(self) -> OrderSignals:
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            token = self._access_token(client)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            ids = self._message_ids(client, headers)
            messages = [self._message(client, headers, message_id) for message_id in ids]
        return order_signals_from(messages)

    def _message_ids(self, client: httpx.Client, headers: Mapping[str, str]) -> tuple[str, ...]:
        found: list[str] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"q": self.query, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(client, headers, f"/gmail/v1/users/{self.user_id}/messages", params)
            for item in payload.get("messages", []) or []:
                if isinstance(item, dict) and item.get("id"):
                    found.append(str(item["id"]))
            page_token = payload.get("nextPageToken")
            if not page_token or len(found) >= MAX_MESSAGES:
                return tuple(found[:MAX_MESSAGES])

    def _message(
        self, client: httpx.Client, headers: Mapping[str, str], message_id: str
    ) -> dict[str, Any]:
        payload = self._get(
            client,
            headers,
            f"/gmail/v1/users/{self.user_id}/messages/{message_id}",
            {"format": "full"},
        )
        body_payload = payload.get("payload")
        body_payload = body_payload if isinstance(body_payload, dict) else {}
        raw_headers = body_payload.get("headers", [])
        message_headers = [entry for entry in raw_headers if isinstance(entry, dict)]
        return {
            "subject": header(message_headers, "Subject"),
            "body": decode_body(body_payload),
        }

    def _get(
        self,
        client: httpx.Client,
        headers: Mapping[str, str],
        path: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            response = client.get(
                f"{self.api_base}{path}", params=dict(params), headers=dict(headers)
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AmbiguousOrderSignal(
                f"Gmail would not answer for {path} ({exc}). Shannon cannot tell which "
                "NAR orders are still awaiting shipment, and will not guess — a partly "
                "read inbox looks exactly like an empty one."
            ) from exc
        if not isinstance(payload, dict):
            raise AmbiguousOrderSignal(
                f"Gmail's answer for {path} was not a record Shannon could read. The "
                "run stops rather than guess at what is on order."
            )
        return payload


__all__ = [
    "DEFAULT_QUERY",
    "GMAIL_READONLY_SCOPE",
    "GmailFixtureClient",
    "GmailLiveClient",
    "OrderEmail",
    "classify",
    "decode_body",
    "find_directives",
    "lines_from_body",
    "oauth_settings",
    "order_signals_from",
]
