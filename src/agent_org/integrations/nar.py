"""North American Rescue — the cart, over Magento's REST API.

narescue.com is Magento 2 with its REST endpoint live, so a cart line is
`POST /rest/V1/carts/mine/items` rather than a form filled in by a
browser. What that buys, beyond speed: the reply names the SKU, the
quantity and the quote it landed in, so "verify every line in the cart
after adding" stops being a screenshot and becomes an assertion.

The one thing to know about the catalogue, established by reading it:

    Individual Patrol Officer Kit (IPOK)   ConfigurableProduct
      hemostatic_agent 149 Gauze (no Hemostatic) -> 80-0167   $57.99
      hemostatic_agent 146 Combat Gauze          -> 80-0168  $110.99
      hemostatic_agent 897 Celox Rapid           -> 80-1787  $111.99

Those are three genuinely different products at three different prices,
not one product with a modifier, and the product page shows the parent's
number whichever is chosen. So the SKU never comes from a page: before a
line is added, `NarCatalogue` asks GraphQL what the SKU *is* — a simple
product, or a variant of a configurable one — and a variant is added as
its parent plus the option that selects it. A parent SKU on its own is
refused: "which variant" is not a question to guess the answer to.

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
GRAPHQL_PATH = "/graphql"

# Every path this client may ever request. Staging a cart needs five; there
# is no sixth, and adding one is a visible line in a diff that a reviewer
# can refuse.
ALLOWED_PATHS = frozenset({TOKEN_PATH, CART_PATH, TOTALS_PATH, ITEMS_PATH, GRAPHQL_PATH})

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


VARIANT_QUERY = """
query ($sku: String!) {
  products(filter: {sku: {eq: $sku}}) {
    items {
      sku
      name
      __typename
      ... on ConfigurableProduct {
        configurable_options { attribute_id attribute_code label }
        variants {
          product { sku name }
          attributes { code value_index }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class CartItem:
    """What has to be posted to put one requested SKU in the cart.

    `sku` is what Magento is asked for — the parent, for a variant — and
    `expect_sku` is what the cart must end up holding. They differ exactly
    when the product is configurable, which is the case the product page
    gets wrong.
    """

    sku: str
    expect_sku: str
    name: str
    options: tuple[tuple[str, int], ...] = ()

    def payload(self, quantity: int, quote_id: str) -> dict[str, Any]:
        item: dict[str, Any] = {"sku": self.sku, "qty": quantity, "quote_id": quote_id}
        if self.options:
            item["product_option"] = {
                "extension_attributes": {
                    "configurable_item_options": [
                        {"option_id": option_id, "option_value": value}
                        for option_id, value in self.options
                    ]
                }
            }
        return {"cartItem": item}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def refuse_unless_safe(method: str, path: str) -> None:
    """The gate every request in this file goes through, before it is sent.

    Three refusals, and no way round them: there is no code here that
    reaches the network without passing through this function first.
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
            f"'{path}' is not one of the paths Shannon may request on "
            "narescue.com. Nothing was sent."
        )


@dataclass
class NarCatalogue:
    """What a SKU actually is, asked of the catalogue rather than a page.

    Public storefront GraphQL: no login, no account, and read-only by the
    same allow-list as everything else here. Asking for a child SKU returns
    the configurable it belongs to, which is what makes the variant
    resolvable at all.
    """

    base_url: str = NAR_BASE_URL
    timeout_seconds: float = 30.0
    transport: httpx.BaseTransport | None = field(default=None, compare=False)

    def resolve(self, sku: str) -> CartItem:
        product = self._match(sku, self._products(sku))
        typename = str(product.get("__typename", ""))
        if typename != "ConfigurableProduct":
            return CartItem(sku=sku, expect_sku=sku, name=str(product.get("name", "")))

        parent = str(product.get("sku", ""))
        if parent == sku:
            raise CartRefusal(
                f"{sku} is a parent product with several variants "
                f"({self._variant_skus(product)}), so 'add {sku}' does not say what to "
                "add. Nothing was added. Name the variant's own SKU in the parts list."
            )
        options = self._options_for(product, sku, parent)
        return CartItem(
            sku=parent,
            expect_sku=sku,
            name=self._variant_name(product, sku) or str(product.get("name", "")),
            options=options,
        )

    def _options_for(
        self, product: dict[str, Any], sku: str, parent: str
    ) -> tuple[tuple[str, int], ...]:
        by_code = {
            str(option.get("attribute_code")): str(option.get("attribute_id"))
            for option in product.get("configurable_options") or []
        }
        for variant in product.get("variants") or []:
            if str((variant.get("product") or {}).get("sku", "")) != sku:
                continue
            chosen: list[tuple[str, int]] = []
            for attribute in variant.get("attributes") or []:
                option_id = by_code.get(str(attribute.get("code")))
                value = attribute.get("value_index")
                if option_id is None or value is None:
                    raise CartRefusal(
                        f"The catalogue describes {sku} as a variant of {parent} but does "
                        f"not say which '{attribute.get('code')}' selects it. Nothing was "
                        "added; this one has to go in the cart by hand."
                    )
                chosen.append((option_id, int(value)))
            if chosen:
                return tuple(chosen)
            break
        raise CartRefusal(
            f"The catalogue lists {sku} under {parent} ({self._variant_skus(product)}) "
            "but says nothing that selects it. Nothing was added; this one has to go "
            "in the cart by hand."
        )

    @staticmethod
    def _match(sku: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """The one product that is this SKU, out of everything that came back.

        A SKU filter on narescue.com is not the exact lookup its name
        suggests: asking for 30-0052 also answers with the kits that have a
        30-0052 inside them, and the first of those is a real product with
        a real name that is simply not the part being ordered. So every
        answer is checked, and only two things count as this SKU: a product
        whose own SKU is it, or a configurable that lists it as one of its
        variants. Anything else is the catalogue talking about something
        else, and is not a reason to say the part number is wrong.
        """
        for item in items:
            if str(item.get("sku", "")) == sku:
                return item
        for item in items:
            variants = item.get("variants") or []
            if any(str((v.get("product") or {}).get("sku", "")) == sku for v in variants):
                return item
        raise CartRefusal(
            f"The narescue.com catalogue has no product with SKU {sku}. It offered "
            f"{NarCatalogue._skus(items)}, which are other products that mention it "
            "rather than the part itself. Nothing was added — check the part number."
        )

    @staticmethod
    def _skus(items: list[dict[str, Any]]) -> str:
        skus = [str(item.get("sku", "")) for item in items]
        return ", ".join(sku for sku in skus if sku) or "nothing"

    @staticmethod
    def _variant_name(product: dict[str, Any], sku: str) -> str:
        for variant in product.get("variants") or []:
            item = variant.get("product") or {}
            if str(item.get("sku", "")) == sku:
                return str(item.get("name", ""))
        return ""

    @staticmethod
    def _variant_skus(product: dict[str, Any]) -> str:
        skus = [
            str((variant.get("product") or {}).get("sku", ""))
            for variant in product.get("variants") or []
        ]
        return ", ".join(sku for sku in skus if sku) or "none listed"

    def _products(self, sku: str) -> list[dict[str, Any]]:
        refuse_unless_safe("POST", GRAPHQL_PATH)
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            try:
                response = client.post(
                    GRAPHQL_PATH,
                    json={"query": VARIANT_QUERY, "variables": {"sku": sku}},
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise CartUnavailable(
                    f"The narescue.com catalogue could not be reached ({exc}), so what "
                    f"{sku} is could not be established. Nothing was added."
                ) from exc
        if response.status_code != 200:
            raise CartUnavailable(
                f"The narescue.com catalogue answered {response.status_code} for {sku}, "
                "so what it is could not be established. Nothing was added."
            )
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise CartUnavailable(
                f"The narescue.com catalogue answered for {sku} with something that is "
                "not JSON. Nothing was added."
            ) from exc
        items = (((body or {}).get("data") or {}).get("products") or {}).get("items") or []
        if not items:
            raise CartRefusal(
                f"The narescue.com catalogue has no product with SKU {sku}. Nothing was "
                "added — a SKU the site does not know is a parts-list error, not a cart "
                "to guess at."
            )
        return [dict(item) for item in items if isinstance(item, dict)]


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
    catalogue: NarCatalogue | None = field(default=None, compare=False)
    _token: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.catalogue is None:
            self.catalogue = NarCatalogue(
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
            )

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

        The SKU is resolved against the catalogue first, so a variant goes
        in as its parent plus the option that selects it rather than as a
        SKU Magento would reject or, worse, silently substitute. The reply
        is then checked against the variant that was asked for: a line that
        came back as a different product is a failure, not a staged line.
        """
        if quantity <= 0:
            raise CartRefusal(f"Asked to add {quantity} of {sku}, which is not a quantity.")
        item = self._catalogue().resolve(sku)
        body = self._json("POST", ITEMS_PATH, json=item.payload(quantity, self._quote_id()))
        staged = str(body.get("sku") or item.expect_sku)
        if staged != item.expect_sku:
            raise CartUnavailable(
                f"{quantity} of {sku} was sent to the cart and narescue.com replied "
                f"with {staged} instead. That line is not what was asked for; check "
                "the cart on the site before ordering anything."
            )
        landed = int(float(body.get("qty", quantity) or quantity))
        if landed != quantity:
            raise CartUnavailable(
                f"{quantity} of {sku} was asked for and narescue.com put {landed} in "
                "the cart. Check the cart on the site before ordering anything."
            )
        return CartLine(
            sku=staged,
            name=str(body.get("name") or item.name),
            quantity=landed,
            price=_decimal(body.get("price")),
            item_id=None if body.get("item_id") is None else str(body["item_id"]),
        )

    def _catalogue(self) -> NarCatalogue:
        if self.catalogue is None:  # pragma: no cover - set in __post_init__
            self.catalogue = NarCatalogue(base_url=self.base_url, transport=self.transport)
        return self.catalogue

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
        refuse_unless_safe(method, path)
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
    "GRAPHQL_PATH",
    "ITEMS_PATH",
    "NAR_BASE_URL",
    "SUPPLIER",
    "TOKEN_PATH",
    "TOTALS_PATH",
    "CartItem",
    "NarCartClient",
    "NarCatalogue",
    "NarFixtureCart",
    "credentials",
    "refuse_unless_safe",
]
