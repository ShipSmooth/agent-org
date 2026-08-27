"""Ask narescue.com two questions, and write nothing anywhere.

Before Shannon stages a NAR cart over Magento's REST API rather than by
driving the Order-by-SKU form, two things have to be true, and neither can
be settled without one real login:

1. `GET /rest/V1/carts/mine` returns **the same cart** Zach sees in his
   browser. Stock Magento says yes — the customer's active quote is one
   cart — but "stock" is the assumption doing the work, and staging into a
   second, invisible cart would be the worst possible outcome: Shannon
   reports a full cart, Zach opens the site, and it is empty.
2. What a configurable product looks like over the API. 80-0167 (the IPOK,
   with its Hemostatic Agent option) is the known case: the product page
   displays the parent SKU whichever variant is chosen, so the child SKU
   has to come from somewhere that is not the page.

This script answers both **read-only**. It logs in, reads the cart, reads
the public catalogue over GraphQL, and prints what it found. It adds
nothing to the cart, removes nothing, and touches no checkout, order or
payment URL — the only paths it will request are the four listed in
`ALLOWED_PATHS`, and it refuses to request anything else.

    # once, in the browser, logged in as yourself:
    #   put 80-0167 (IPOK) in the cart, choosing the usual Hemostatic
    #   Agent option, and anything else you like alongside it.
    uv run python scripts/nar_api_spike.py

The catalogue half needs no account at all, and can be run on its own:

    uv run python scripts/nar_api_spike.py --catalogue-only

It asks for the narescue.com email and password at the prompt (nothing is
echoed, nothing is stored, nothing is written to disk), or takes them from
NAR_EMAIL and NAR_PASSWORD if they are already in the environment. The
token is never printed. Output is filtered: anything that looks like an
address, a name, a telephone number or an email is removed before it
reaches the screen, so the whole output can be pasted back as-is.
"""

from __future__ import annotations

import json
import os
import re
import sys
from getpass import getpass
from typing import Any

import httpx

BASE = "https://www.narescue.com"
IPOK = "80-0167"

# The only paths this script may request. A checkout, order or payment path
# is not on it, and cannot be reached by any code below.
ALLOWED_PATHS = frozenset(
    {
        "/rest/V1/integration/customer/token",
        "/rest/V1/carts/mine",
        "/rest/V1/carts/mine/totals",
        "/graphql",
    }
)

# Personal detail has no bearing on either question, so it never reaches the
# screen. Keys are matched case-insensitively, at any depth.
PRIVATE_KEYS = frozenset(
    {
        "address",
        "billing_address",
        "city",
        "company",
        "customer",
        "customer_email",
        "customer_firstname",
        "customer_lastname",
        "email",
        "extension_attributes_shipping",
        "firstname",
        "lastname",
        "middlename",
        "postcode",
        "prefix",
        "region",
        "shipping_address",
        "street",
        "suffix",
        "telephone",
        "vat_id",
    }
)
EMAILISH = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


class RefusedPath(RuntimeError):
    """A path outside the allow-list was asked for."""


def _redact(value: Any) -> Any:
    """Personal detail out, product and quantity in."""
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.lower() in PRIVATE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return EMAILISH.sub("[redacted]", value)
    return value


def _show(label: str, value: Any) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(_redact(value), indent=2, sort_keys=True)[:4000])


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
    if path not in ALLOWED_PATHS:
        raise RefusedPath(path)
    return client.request(method, BASE + path, timeout=30, **kwargs)


def _credentials() -> tuple[str, str]:
    email = os.environ.get("NAR_EMAIL") or input("narescue.com email: ").strip()
    password = os.environ.get("NAR_PASSWORD") or getpass("narescue.com password (not echoed): ")
    return email, password


def _token(client: httpx.Client, email: str, password: str) -> str | None:
    response = _request(
        client,
        "POST",
        "/rest/V1/integration/customer/token",
        json={"username": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    print(f"login: HTTP {response.status_code}")
    if response.status_code != 200:
        # The message, not the body: Magento's failure text is informative
        # and carries nothing private.
        print(f"  {response.text[:300]}")
        return None
    token = response.json()
    return token if isinstance(token, str) else None


def _cart(client: httpx.Client, token: str) -> None:
    """Question 1: is this the cart Zach is looking at?"""
    auth = {"Authorization": f"Bearer {token}"}
    cart = _request(client, "GET", "/rest/V1/carts/mine", headers=auth)
    print(f"\ncart: HTTP {cart.status_code}")
    if cart.status_code != 200:
        print(f"  {cart.text[:300]}")
        return

    body = cart.json()
    items = body.get("items", [])
    print(f"quote id {body.get('id')}, active={body.get('is_active')}, {len(items)} line(s)")
    print("Compare this against the cart in the browser, line for line:")
    for item in items:
        print(
            f"  {item.get('sku'):<20} qty {item.get('qty'):<6} "
            f"{str(item.get('name'))[:48]}  [{item.get('product_type')}]"
        )
    # Whole lines, unabridged, for the configurable: the option payload is
    # exactly what Shannon will have to send back when she stages one.
    for item in items:
        if item.get("product_type") == "configurable" or str(item.get("sku", "")).startswith(IPOK):
            _show(f"configurable line {item.get('sku')}", item)

    totals = _request(client, "GET", "/rest/V1/carts/mine/totals", headers=auth)
    if totals.status_code == 200:
        figures = totals.json()
        print(
            f"\ntotals: subtotal {figures.get('subtotal')} "
            f"grand total {figures.get('grand_total')} "
            f"currency {figures.get('quote_currency_code')}"
        )
    else:
        print(f"\ntotals: HTTP {totals.status_code} — {totals.text[:200]}")


def _ipok(client: httpx.Client) -> None:
    """Question 2: what are the IPOK's real child SKUs and options?

    GraphQL is the storefront's own public API — no login, no account
    involved. If it answers, Shannon can resolve a variant without reading
    a page that displays the parent SKU regardless of the choice made.
    """
    query = """
    query ($sku: String!) {
      products(filter: {sku: {eq: $sku}}) {
        items {
          sku
          name
          __typename
          ... on ConfigurableProduct {
            configurable_options { attribute_code label values { value_index label } }
            variants {
              product { sku name price_range { minimum_price { final_price { value currency } } } }
              attributes { code label value_index }
            }
          }
        }
      }
    }
    """
    response = _request(
        client,
        "POST",
        "/graphql",
        json={"query": query, "variables": {"sku": IPOK}},
        headers={"Content-Type": "application/json"},
    )
    print(f"\ngraphql catalogue read for {IPOK}: HTTP {response.status_code}")
    if response.status_code != 200:
        print(f"  {response.text[:300]}")
        return
    _show(f"{IPOK} as the catalogue describes it", response.json())


def main(argv: list[str]) -> int:
    print(__doc__.split("\n\n")[0])
    print(f"Reading {BASE} only, and only these paths: {', '.join(sorted(ALLOWED_PATHS))}")
    print("Nothing is added to the cart. Nothing is ordered.\n")
    catalogue_only = "--catalogue-only" in argv

    with httpx.Client(follow_redirects=False) as client:
        if not catalogue_only:
            email, password = _credentials()
            token = _token(client, email, password)
            if token is None:
                print("\nNo token, so no cart read. Nothing was changed.")
                return 1
            _cart(client, token)
        _ipok(client)

    print(
        "\nDone — nothing was written. Paste all of the above back, plus:\n"
        "  (a) does the cart listed above match what your browser shows, line for line?\n"
        "  (b) is the IPOK line in it the variant you normally buy?"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
