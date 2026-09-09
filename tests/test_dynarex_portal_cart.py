"""Shannon fills the Dynarex cart through Quick Order, and cannot buy.

dynarex.com is a commercebuild portal rather than an API, so every claim
this client makes about the portal is checked here against a local one
that behaves the way the signed-in spike found the real one behaving: the
login page carries the search box twice before the login, the search is a
*contains* search that answers 3161 with 33161 and 43161 as well, and
Quick Order is a part-number box with a quantity beside it.

Nothing here touches dynarex.com, and the portal below has a Checkout
button on the Quick Order page on purpose — the one button Shannon must
walk past.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from agent_org.integrations.carts import CartRefusal, CartUnavailable, SupplierCart
from agent_org.integrations.dynarex import (
    DynarexFixtureCart,
    DynarexPortalCart,
    credentials,
    exact_code,
    refuse_unless_safe,
)

PASSWORD = "the-right-one"
EMAIL = "zach@example.test"

CATALOGUE = {
    "3161": ("Krinkle Gauze Roll - Sterile", "12.34"),
    "3553": ("Sterile Gauze Pad", "8.10"),
}
# What the portal's contains-search answers 3161 with, in its own order:
# the part itself is not first, and two of these are real other products.
NEIGHBOURS = {
    "3161": ["33161", "3161", "43161"],
    "3553": ["3553"],
    "9999": ["99991"],
}

SEARCH_FORM = """
<form action="/product_search/" method="GET" id="search_mini_form">
  <input name="q" type="text" id="search" placeholder="Search">
  <button type="submit">Go</button>
</form>
"""


def _login_page() -> str:
    return f"""<html><body>Welcome Guest
{SEARCH_FORM}{SEARCH_FORM}
<form action="/user/login" method="POST">
  <input name="login_username" type="text" id="email">
  <input name="login_password" type="password" id="pass">
  <button type="submit">Login</button>
</form>
</body></html>"""


class _Portal(BaseHTTPRequestHandler):
    """Enough of commercebuild to stage a cart against."""

    cart: ClassVar[dict[str, int]] = {}
    clicked: ClassVar[list[str]] = []
    captcha: ClassVar[bool] = False

    def _send(self, body: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def _cart_page(self) -> str:
        if not self.cart:
            return "<html><body>Sign Out. Your cart is currently empty.</body></html>"
        rows = "".join(
            f"<tr class='item'><td>{CATALOGUE[sku][0]} Code: {sku} "
            f"${CATALOGUE[sku][1]}</td>"
            f"<td><input name='qty[{sku}]' value='{quantity}'></td></tr>"
            for sku, quantity in self.cart.items()
        )
        total = sum(Decimal(CATALOGUE[sku][1]) * qty for sku, qty in self.cart.items())
        return (
            f"<html><body>Sign Out<table>{rows}</table><p>Grand Total: ${total}</p></body></html>"
        )

    def _quick_order_page(self) -> str:
        return f"""<html><body>Sign Out
{SEARCH_FORM}
<form action="/cart/quickorders" method="POST">
  <table><tr>
    <td><input type="text" name="item_code" placeholder="Item Code"></td>
    <td><input type="text" name="qty" value=""></td>
  </tr></table>
  <button type="submit" name="checkout">Checkout</button>
  <button type="submit" name="add">Add to Cart</button>
</form>
</body></html>"""

    def _search_page(self, sku: str) -> str:
        results = "".join(
            f"<div class='product'>Product {code} Code: {code} $1.00</div>"
            for code in NEIGHBOURS.get(sku, [])
        )
        return f"<html><body>Sign Out{results}</body></html>"

    def do_GET(self) -> None:
        if self.captcha:
            self._send("<html><head><title>Checking your browser</title></head><body/></html>")
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/cart":
            self._send(self._cart_page())
        elif path == "/cart/quickorders":
            self._send(self._quick_order_page())
        elif path == "/product_search":
            self._send(self._search_page(parse_qs(parsed.query).get("q", [""])[0]))
        else:
            self._send(_login_page())

    def do_POST(self) -> None:
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        path = urlparse(self.path).path.rstrip("/")
        if path == "/cart/quickorders":
            self.clicked.append("checkout" if "checkout" in form else "add")
            sku = form.get("item_code", [""])[0]
            self.cart[sku] = self.cart.get(sku, 0) + int(form.get("qty", ["0"])[0])
            self._send(self._cart_page())
            return
        if form.get("login_password", [""])[0] == PASSWORD:
            self._send("<html><body>My Account — Sign Out</body></html>")
        else:
            self._send("<html><body><div class='error-msg'>Invalid login or password.</div></body>")

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def portal() -> Iterator[str]:
    _Portal.cart = {}
    _Portal.clicked = []
    _Portal.captcha = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as missing:
            pytest.skip(f"no browser: {missing}")
        opened = browser.new_page()
        yield opened
        browser.close()


@pytest.fixture
def cart(portal: str, page: Page, monkeypatch: pytest.MonkeyPatch) -> DynarexPortalCart:
    monkeypatch.setenv("DYNAREX_EMAIL", EMAIL)
    monkeypatch.setenv("DYNAREX_PASSWORD", PASSWORD)
    return DynarexPortalCart(base_url=portal, page=page)


def test_an_empty_cart_is_read_as_empty_and_a_full_one_line_by_line(
    cart: DynarexPortalCart,
) -> None:
    assert cart.read_cart().lines == ()

    _Portal.cart = {"3161": 4}
    read = cart.read_cart()

    assert [(line.sku, line.quantity, line.price) for line in read.lines] == [
        ("3161", 4, Decimal("12.34"))
    ]
    assert read.grand_total == Decimal("49.36")


def test_a_line_goes_in_through_quick_order_and_is_read_back_out(
    cart: DynarexPortalCart,
) -> None:
    _Portal.cart = {"3553": 2}

    line = cart.add_line("3161", 5)

    assert (line.sku, line.quantity) == ("3161", 5)
    assert _Portal.cart == {"3553": 2, "3161": 5}, "what was already there stays there"


def test_the_checkout_button_on_the_quick_order_page_is_never_the_one_clicked(
    cart: DynarexPortalCart,
) -> None:
    """The portal is allowed to offer checkout; Shannon may not take it."""
    cart.add_line("3161", 1)

    assert _Portal.clicked == ["add"]


def test_a_part_the_portal_only_answers_with_its_neighbours_is_refused(
    cart: DynarexPortalCart,
) -> None:
    """3161's search answers 33161 and 43161 too — the NAR bug, in a portal."""
    with pytest.raises(CartRefusal, match="no product whose code is 9999"):
        cart.add_line("9999", 1)

    assert _Portal.cart == {}


def test_a_cart_that_does_not_hold_what_went_in_is_a_failure_not_a_success(
    cart: DynarexPortalCart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification is the whole point: a portal that quietly doubled a
    line would otherwise be reported to Zach as a clean add."""
    monkeypatch.setattr(
        DynarexPortalCart,
        "_submit",
        lambda self, page: _Portal.cart.update({"3161": 11}),
    )

    with pytest.raises(CartUnavailable, match="holds 11 of it where 3 was expected"):
        cart.add_line("3161", 3)


def test_a_login_the_portal_refuses_says_so_in_the_portals_words(
    portal: str, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DYNAREX_EMAIL", EMAIL)
    monkeypatch.setenv("DYNAREX_PASSWORD", "wrong")

    with pytest.raises(CartUnavailable, match="Invalid login or password"):
        DynarexPortalCart(base_url=portal, page=page).read_cart()


def test_a_captcha_stops_the_run_rather_than_being_worked_around(
    cart: DynarexPortalCart,
) -> None:
    _Portal.captcha = True

    with pytest.raises(CartUnavailable, match="captcha"):
        cart.read_cart()


def test_a_missing_login_is_a_refusal_to_read_rather_than_an_empty_cart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DYNAREX_EMAIL", raising=False)
    monkeypatch.delenv("DYNAREX_PASSWORD", raising=False)

    with pytest.raises(CartUnavailable, match="DYNAREX_EMAIL and DYNAREX_PASSWORD"):
        credentials()


def test_only_the_four_pages_staging_needs_can_be_opened() -> None:
    for path in ("/user/login", "/cart", "/cart/quickorders", "/product_search/"):
        refuse_unless_safe(path)
    for path in ("/checkout", "/checkout/onepage/success", "/sales/order/history", "/billing"):
        with pytest.raises(CartRefusal):
            refuse_unless_safe(path)
    with pytest.raises(CartRefusal, match="not one of the pages"):
        refuse_unless_safe("/user/account")


def test_the_only_thing_this_client_can_do_to_a_cart_is_read_it_and_add_to_it() -> None:
    """The permanent constraint, as code: there is no method to call."""
    verbs = {name for name in dir(DynarexPortalCart) if not name.startswith("_")}

    assert verbs == {"add_line", "read_cart", "base_url", "credentials_prefix"} | {
        "headless",
        "page",
        "supplier",
        "timeout_ms",
    }


def test_an_exact_code_is_the_part_itself_and_nothing_else() -> None:
    results = ["Nasal Oxygen Code: 33161", "Krinkle Gauze Code: 3161", "Loop Code: 43161"]

    assert exact_code("3161", results) == "Krinkle Gauze Code: 3161"
    assert exact_code("316", results) is None


def test_the_saved_dynarex_cart_reads_but_refuses_to_be_added_to(tmp_path: Path) -> None:
    saved = DynarexFixtureCart(fixture_dir=Path("tests/fixtures/golden/data"))

    assert saved.read_cart().supplier == "dynarex"
    with pytest.raises(CartRefusal, match="saved copy of the cart"):
        saved.add_line("3161", 1)

    missing: SupplierCart = DynarexFixtureCart(fixture_dir=tmp_path)
    with pytest.raises(CartUnavailable, match="is missing"):
        missing.read_cart()
