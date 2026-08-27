"""North American Rescue — the cart, over Magento's REST API.

narescue.com is Magento 2 with its REST endpoint live, so a cart line is
`POST /rest/V1/carts/mine/items` rather than a form filled in by a
browser. What that buys, beyond speed: the reply names the SKU, the
quantity and the quote it landed in, so "verify every line in the cart
after adding" stops being a screenshot and becomes an assertion.

The one thing to know about the catalogue, established by reading it:

    80-0168-s  Individual Patrol Officer Kit (IPOK)   ConfigurableProduct
      hemostatic_agent 149 Gauze (no Hemostatic) -> 80-0167
      hemostatic_agent 146 Combat Gauze          -> 80-0168
      hemostatic_agent 897 Celox Rapid           -> 80-1787

`80-0167` is the child we buy, not a parent needing an option payload, so
it is added like any other SKU. The product page's "ITEM #: 80-0168-s" is
the parent it hangs off, which is exactly the trap the operational runbook
warns about: the page cannot be trusted for the SKU, and the cart can.

Never checking out is enforced three ways in this file, none of them a
setting:

1. `ALLOWED_PATHS` is a closed set. Anything else raises `CartRefusal`
   before a socket is opened.
2. `FORBIDDEN` is checked as well, so a path added to the allow-list in a
   future edit still cannot be one that orders, pays or checks out.
   Magento places an order with `PUT /rest/V1/carts/mine/order`, which
   this refuses on both counts.
3. Only GET and POST are ever sent. PUT and DELETE, which are how Magento
   places an order and empties a cart, are refused by method.

There is no credential in this file. `NAR_EMAIL` and `NAR_PASSWORD` are
read from the environment at the moment of use, under the entity's own
prefix, and the token is held in memory for one run and never logged.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from agent_org.integrations.carts import Cart, CartLine, CartRefusal, CartUnavailable

NAR_BASE_URL = "https://www.narescue.com"
SUPPLIER = "nar"

TOKEN_PATH = "/rest/V1/integration/customer/token"
CART_PATH = "/rest/V1/carts/mine"
TOTALS_PATH = "/rest/V1/carts/mine/totals"
ITEMS_PATH = "/rest/V1/carts/mine/items"

# Every path this client may ever request. Staging a cart needs four; there
# is no fifth, and adding one is a visible line in a diff that a reviewer
# can refuse.
ALLOWED_PATHS = frozenset({TOKEN_PATH, CART_PATH, TOTALS_PATH, ITEMS_PATH})

# Belt as well as braces: a word that appears in any path that buys
# something. `/rest/V1/carts/mine/order`, `/checkout/onepage/`,
# `/paypal/express/start` and the payment-information endpoints all match.
FORBIDDEN = re.compile(
    r"checkout|/order\b|order/|payment|paypal|purchase|place|billing", re.IGNORECASE
)
SAFE_METHODS = frozenset({"GET", "POST"})

EMAIL_VAR = "NAR_EMAIL"
PASSWORD_VAR = "NAR_PASSWORD"


def credentials(credentials_prefix: str = "") -> tuple[str, str]:
    """The NAR login, from the environment and nowhere else.

    Never logged, never defaulted, never written to a report. The names are
    the entity's own — `ITHRIVE_NAR_EMAIL` — so one business's login can
    never be picked up by another's run.
    """
    names = (f"{credentials_prefix}{EMAIL_VAR}", f"{credentials_prefix}{PASSWORD_VAR}")
    email, password = (os.environ.get(name, "").strip() for name in names)
    if not email or not password:
        missing = " and ".join(
            name for name, value in zip(names, (email, password), strict=True) if not value
        )
        raise CartUnavailable(
            f"{missing} is not set, so the narescue.com cart cannot be read. "
            "Nothing has been staged. Put the login in the environment (or in "
            ".env) and run again."
        )
    return email, password


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class NarCartClient:
    """Read the narescue.com cart, and add a line to it. That is all it can do."""

    supplier: str = SUPPLIER
    credentials_prefix: str = ""
    base_url: str = NAR_BASE_URL
    timeout_seconds: float = 30.0
    # Injected in tests, which is how every path below is exercised without
    # a credential and without a network.
    transport: httpx.BaseTransport | None = field(default=None, compare=False)
    _token: str | None = field(default=None, init=False, repr=False, compare=False)

    def read_cart(self) -> Cart:
        body = self._json("GET", CART_PATH)
        lines: list[CartLine] = []
        for item in body.get("items") or []:
            lines.append(
                CartLine(
                    sku=str(item.get("sku", "")),
                    name=str(item.get("name", "")),
                    quantity=int(float(item.get("qty", 0) or 0)),
                    price=_decimal(item.get("price")),
                    item_id=None if item.get("item_id") is None else str(item["item_id"]),
                )
            )
        total, currency = self._totals()
        return Cart(
            supplier=self.supplier,
            cart_id=str(body.get("id", "")),
            lines=tuple(lines),
            grand_total=total,
            currency=currency,
        )

    def add_line(self, sku: str, quantity: int) -> CartLine:
        """Put one line in the cart, and read back what actually landed.

        The reply is Magento's own view of the line it created, so the SKU
        it reports is the SKU in the cart — not the one on the product
        page, which for a configurable product is its parent's.
        """
        if quantity <= 0:
            raise CartRefusal(f"Asked to add {quantity} of {sku}, which is not a quantity.")
        body = self._json(
            "POST",
            ITEMS_PATH,
            json={"cartItem": {"sku": sku, "qty": quantity, "quote_id": self._quote_id()}},
        )
        return CartLine(
            sku=str(body.get("sku", sku)),
            name=str(body.get("name", "")),
            quantity=int(float(body.get("qty", quantity) or quantity)),
            price=_decimal(body.get("price")),
            item_id=None if body.get("item_id") is None else str(body["item_id"]),
        )

    def _quote_id(self) -> str:
        body = self._json("GET", CART_PATH)
        return str(body.get("id", ""))

    def _totals(self) -> tuple[Decimal | None, str]:
        body = self._json("GET", TOTALS_PATH)
        return _decimal(body.get("grand_total")), str(body.get("quote_currency_code") or "USD")

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path, headers=self._auth(), **kwargs)
        if response.status_code >= 400:
            raise CartUnavailable(
                f"narescue.com answered {response.status_code} for {path}. "
                "Nothing was added to the cart, and no line is reported as staged."
            )
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise CartUnavailable(
                f"narescue.com answered {path} with something that is not JSON, so "
                "the cart cannot be read. Nothing was staged."
            ) from exc
        return body if isinstance(body, dict) else {"items": body}

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            self._token = self._login()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _login(self) -> str:
        email, password = credentials(self.credentials_prefix)
        response = self._request(
            "POST",
            TOKEN_PATH,
            json={"username": email, "password": password},
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            # The status, never the body: a login reply is the one place a
            # site is most likely to echo something private back.
            raise CartUnavailable(
                f"narescue.com refused the login ({response.status_code}). Nothing "
                "was read and nothing was staged. Check the login in the environment."
            )
        token = response.json()
        if not isinstance(token, str) or not token:
            raise CartUnavailable("narescue.com returned no session token, so nothing was staged.")
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """The gate every request goes through, including the login.

        Three refusals, checked before anything is sent. A caller cannot
        talk itself past them, because there is no other way out of this
        class to the network.
        """
        if method.upper() not in SAFE_METHODS:
            raise CartRefusal(
                f"{method} is how Magento places an order and empties a cart. "
                f"Shannon reads and adds; she never {method}s."
            )
        if FORBIDDEN.search(path):
            raise CartRefusal(
                f"'{path}' is a checkout, order or payment path. Shannon stages a "
                "cart and stops there — permanently, at every tier."
            )
        if path not in ALLOWED_PATHS:
            raise CartRefusal(
                f"'{path}' is not one of the four paths Shannon may request on "
                "narescue.com. Nothing was sent."
            )
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            try:
                return client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise CartUnavailable(
                    f"narescue.com could not be reached ({exc}). Nothing was staged."
                ) from exc


@dataclass
class NarFixtureCart:
    """The cart as a saved JSON file, for a dry run with no account.

    Same interface, and `add_line` is absent by construction: a fixture
    cart has nothing to add to. A dry run never calls it anyway — that is
    what makes the dry run a dry run.
    """

    fixture_dir: Path
    supplier: str = SUPPLIER

    def read_cart(self) -> Cart:
        path = self.fixture_dir / "nar_cart.json"
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
                price=_decimal(item.get("price")),
            )
            for item in body.get("items", [])
        )
        return Cart(
            supplier=self.supplier,
            cart_id=str(body.get("id", "fixture")),
            lines=lines,
            grand_total=_decimal(body.get("grand_total")),
        )

    def add_line(self, sku: str, quantity: int) -> CartLine:
        raise CartRefusal(
            f"This is a saved copy of the cart, not the cart. {quantity} of {sku} "
            "was not added anywhere."
        )


__all__ = [
    "ALLOWED_PATHS",
    "CART_PATH",
    "FORBIDDEN",
    "ITEMS_PATH",
    "NAR_BASE_URL",
    "SUPPLIER",
    "TOKEN_PATH",
    "TOTALS_PATH",
    "NarCartClient",
    "NarFixtureCart",
    "credentials",
]
