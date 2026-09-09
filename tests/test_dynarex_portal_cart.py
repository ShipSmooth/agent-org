"""Shannon fills the Dynarex cart through Quick Order, and cannot buy.

dynarex.com is a commercebuild portal rather than an API, so every claim
this client makes about the portal is checked here against a local one
that behaves the way the signed-in spike found the real one behaving: the
login page carries the search box twice before the login, the search is a
*contains* search that answers 3161 with 33161 and 43161 as well, and
Quick Order is a part-number box with a quantity beside it.

The Quick Order page below is shaped like the live one, watched by hand
in the browser after two rounds of guessing at it from markup:

* twenty blank rows, each with its search box and its quantity box there
  from page load — neither is injected later;
* typing drops an autocomplete under the row, and it is the contains
  search again: 3161 offers 33161 and 43161 as well;
* **typing alone does nothing**. Clicking a suggestion is what acts, and
  the line is in the cart at quantity 1 from that click, before any
  quantity has been chosen — so the tests below can leave the cart
  holding 1 of a part, and one of them does, on purpose;
* the quantity box is then overwritten, which edits the line that is
  already there. There is no Add button anywhere on the page.

Nothing here touches dynarex.com, and the portal below carries "Add Row",
"View Cart" and "Proceed to Checkout" on purpose — the buttons Shannon
walks past, none of which she may click.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from agent_org.integrations.carts import CartRefusal, CartUnavailable, SupplierCart
from agent_org.integrations.dynarex import (
    QUICK_ORDER_ROW_JS,
    SUGGESTIONS_JS,
    DynarexFixtureCart,
    DynarexPortalCart,
    credentials,
    exact_code,
    exact_suggestion,
    refuse_unless_safe,
)

PASSWORD = "the-right-one"
EMAIL = "zach@example.test"

CATALOGUE = {
    "3161": ("Krinkle Gauze Roll - Sterile", "12.34"),
    "3553": ("Sterile Gauze Pad", "8.10"),
    "33161": ("Nasal Oxygen Cannula", "5.00"),
    "43161": ("Suction Tubing Loop", "7.25"),
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

# The live page's behaviour, copied rather than guessed at: a dropdown
# after a pause, a click that both fills the row in and puts the line in
# the cart at 1, and a quantity box that edits that line afterwards.
# Everything posts by script; nothing on the page is a form.
QUICK_ORDER_SCRIPT = """
const NAMES = %(names)s;
const OFFERS = %(offers)s;
const FILLS_ROW = %(fills_row)s;

const send = (which, code, delta) => {
  const body = new URLSearchParams(
      {which: which, item_code: code, delta: String(delta)});
  fetch('/cart/quickorders', {method: 'POST', body: body});
};

const choose = (row, code) => {
  // Two seconds on the real portal; the line is in the cart from here,
  // whatever happens to the rest of the row afterwards.
  setTimeout(() => {
    send('select', code, 1);
    if (!FILLS_ROW) return;
    row.dataset.code = code;
    row.dataset.sent = '1';
    row.querySelector('.desc').textContent = NAMES[code] + ' (' + code + ')';
    row.querySelector('.price').textContent = '$1.00 / CS';
    row.querySelector('.qty').value = '1';
    row.querySelector('.qo-suggest').innerHTML = '';
  }, 400);
};

for (const row of document.querySelectorAll('.rTableRow')) {
  const box = row.querySelector('.qo-box');
  const list = row.querySelector('.qo-suggest');
  const qty = row.querySelector('.qty');
  box.addEventListener('input', () => {
    list.innerHTML = '';
    const typed = box.value.trim();
    if (typed.length < 3) return;
    setTimeout(() => {
      for (const code of (OFFERS[typed] || [])) {
        const item = document.createElement('li');
        // As the portal writes it: the matched digits are their own
        // highlighted span, so 33161 is '3' plus a highlighted '3161'.
        const at = code.indexOf(typed);
        item.innerHTML = "<span class='name'>" + NAMES[code] + "</span> (" +
            code.slice(0, at) + "<span class='hl'>" + typed + '</span>' +
            code.slice(at + typed.length) + ') <span class="p">$1.00</span>';
        item.addEventListener('click', () => choose(row, code));
        list.appendChild(item);
      }
    }, 200);
  });
  qty.addEventListener('change', () => {
    if (!row.dataset.code) return;
    send('qty', row.dataset.code, Number(qty.value) - Number(row.dataset.sent));
    row.dataset.sent = qty.value;
  });
}

for (const [id, which] of [['addrow', 'add row'], ['viewcart', 'view cart'],
                           ['co', 'checkout']]) {
  document.getElementById(id).addEventListener(
      'click', () => send(which, '-', 0));
}
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
    quick_order_row: ClassVar[bool] = True
    # Whether the autocomplete offers the part itself, or only its
    # neighbours, and whether the row answers a click at all.
    offers_the_part: ClassVar[bool] = True
    fills_row: ClassVar[bool] = True

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
        """Twenty blank rows, both boxes of each there from the start."""
        row = """<div class="rTableRow">
  <div class="rTableCell search-box">
    <input type="text" class="qo-box" autocomplete="off">
    <span class="mag">&#128269;</span>
    <ul class="qo-suggest"></ul>
  </div>
  <div class="rTableCell desc"></div>
  <div class="rTableCell price"></div>
  <div class="rTableCell qty-cell"><input class="qty" name="qty" value=""></div>
</div>"""
        offers = {
            typed: [code for code in codes if code != typed or self.offers_the_part]
            for typed, codes in NEIGHBOURS.items()
        }
        script = QUICK_ORDER_SCRIPT % {
            "names": dumps({code: name for code, (name, _) in CATALOGUE.items()}),
            "offers": dumps(offers),
            "fills_row": "true" if self.fills_row else "false",
        }
        return f"""<html><body class="quick-orders-page">Sign Out
{SEARCH_FORM}
<div class="rTable">{row * 20}</div>
<p>Sub-Total: $0.00</p>
<button id="addrow">Add Row</button>
<button id="viewcart">View Cart</button>
<button id="co">Proceed to Checkout</button>
<script>
{script}
</script>
</body></html>"""

    def _search_boxes_only_page(self) -> str:
        """The page as the first live run described it: no row at all."""
        return f"<html><body>Sign Out{SEARCH_FORM}{SEARCH_FORM}</body></html>"

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
            self._send(
                self._quick_order_page() if self.quick_order_row else self._search_boxes_only_page()
            )
        elif path == "/product_search":
            self._send(self._search_page(parse_qs(parsed.query).get("q", [""])[0]))
        else:
            self._send(_login_page())

    def do_POST(self) -> None:
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        path = urlparse(self.path).path.rstrip("/")
        if path == "/cart/quickorders":
            which, sku = form.get("which", [""])[0], form.get("item_code", [""])[0]
            self.clicked.append(f"{which} {sku}".strip())
            held = self.cart.get(sku, 0) + int(form.get("delta", ["0"])[0])
            if held:
                self.cart[sku] = held
            else:
                self.cart.pop(sku, None)
            self._send("ok")
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
    _Portal.quick_order_row = True
    _Portal.offers_the_part = True
    _Portal.fills_row = True
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
    return DynarexPortalCart(base_url=portal, page=page, settle_ms=5_000)


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


def test_the_line_lands_at_one_on_selection_and_the_quantity_is_a_second_act(
    cart: DynarexPortalCart,
) -> None:
    """The order of events matters, because a failure between the two
    leaves a real line in a real cart at the wrong quantity."""
    cart.add_line("3161", 5)

    assert _Portal.clicked == ["select 3161", "qty 3161"]


def test_nothing_on_the_page_that_offers_to_buy_is_ever_clicked(
    cart: DynarexPortalCart,
) -> None:
    """Add Row, View Cart and Proceed to Checkout are all on the page.
    Only a dropdown suggestion and a quantity box are touched."""
    cart.add_line("3161", 1)

    # One of one: the selection is the whole add, and rewriting the
    # quantity box with the 1 already in it changes nothing.
    assert _Portal.clicked == ["select 3161"]


def test_a_dropdown_that_only_offers_the_neighbours_is_left_unclicked(
    cart: DynarexPortalCart,
) -> None:
    """Typing does nothing on this page, so refusing to click is enough:
    33161 and 43161 are real products, and neither of them is 3161."""
    _Portal.offers_the_part = False

    with pytest.raises(CartUnavailable, match="never offered 3161 itself") as refused:
        cart.add_line("3161", 2)

    assert "33161" in str(refused.value), "the refusal quotes what it was offered instead"
    assert _Portal.cart == {}
    assert _Portal.clicked == [], "typing alone adds nothing, so nothing was added"


def test_a_suggestion_is_read_whole_and_not_just_its_highlighted_digits(
    portal: str, page: Page
) -> None:
    """The portal highlights the digits that matched, in their own span,
    so 33161 is written as a plain 3 and a highlighted 3161. Reading the
    innermost node read all three suggestions as '3161' and Shannon could
    not tell the part from its neighbours at all."""
    page.goto(f"{portal}/cart/quickorders")
    assert page.evaluate(QUICK_ORDER_ROW_JS) is not None
    page.locator("[data-shannon-sku]").first.press_sequentially("3161", delay=20)
    page.wait_for_timeout(600)

    offered = [dict(option) for option in page.evaluate(SUGGESTIONS_JS, "3161")]

    assert [option["text"] for option in offered] == [
        "Nasal Oxygen Cannula (33161) $1.00",
        "Krinkle Gauze Roll - Sterile (3161) $1.00",
        "Suction Tubing Loop (43161) $1.00",
    ]
    assert exact_suggestion("3161", offered) == offered[1]


def test_a_row_that_never_fills_itself_in_says_the_line_may_be_there_at_one(
    cart: DynarexPortalCart,
) -> None:
    """The click is the committing act. If the portal stops answering after
    it, the cart is not clean, and saying "nothing was added" would be a
    lie Zach would act on."""
    _Portal.fills_row = False

    with pytest.raises(CartUnavailable, match="may now hold 1 of 3161") as refused:
        cart.add_line("3161", 4)

    assert "correct it by hand" in str(refused.value)
    assert "Nothing has been checked out" in str(refused.value)
    assert _Portal.cart == {"3161": 1}, "which is exactly what the refusal warns about"


def test_the_search_box_is_never_mistaken_for_the_part_number_box(
    cart: DynarexPortalCart,
) -> None:
    """Both boxes are empty text boxes; only one of them orders anything."""
    cart.add_line("3161", 2)

    assert _Portal.cart == {"3161": 2}


def test_a_quick_order_page_with_no_row_says_what_it_did_hold_instead(
    cart: DynarexPortalCart,
) -> None:
    """The refusal names fields rather than forms: the live row belongs to
    no form, so listing forms described a page nobody had looked at."""
    _Portal.quick_order_row = False

    with pytest.raises(CartUnavailable, match="holds these fields") as refused:
        cart.add_line("3161", 1)

    assert "was not typed and nothing was added" in str(refused.value)
    assert _Portal.cart == {}


def test_a_part_the_portal_only_answers_with_its_neighbours_is_refused(
    cart: DynarexPortalCart,
) -> None:
    """3161's search answers 33161 and 43161 too — the NAR bug, in a portal."""
    with pytest.raises(CartRefusal, match="no product whose code is 9999"):
        cart.add_line("9999", 1)

    assert _Portal.cart == {}


def test_a_line_left_at_one_by_a_lost_quantity_is_reported_as_exactly_that(
    cart: DynarexPortalCart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this page makes possible: the quantity edit never
    happens, so the cart holds 1 rather than nothing. "Line exists but
    wrong quantity" has to read differently from "line absent"."""
    monkeypatch.setattr(DynarexPortalCart, "_set_quantity", lambda self, page, quantity: None)

    with pytest.raises(CartUnavailable, match="holds 1 of it where 5 was expected") as refused:
        cart.add_line("3161", 5)

    assert "cart now holds 1 of 3161 (it should be 5)" in str(refused.value)
    assert "correct it by hand" in str(refused.value)
    assert _Portal.cart == {"3161": 1}


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
        "settle_ms",
        "supplier",
        "timeout_ms",
    }


def test_an_exact_code_is_the_part_itself_and_nothing_else() -> None:
    results = ["Nasal Oxygen Code: 33161", "Krinkle Gauze Code: 3161", "Loop Code: 43161"]

    assert exact_code("3161", results) == "Krinkle Gauze Code: 3161"
    assert exact_code("316", results) is None


def test_a_suggestion_counts_only_when_the_code_in_it_is_the_part() -> None:
    """How the dropdown writes a code: a suffix in brackets."""
    offered = [
        {"index": 0, "text": "Nasal Oxygen Cannula (33161)"},
        {"index": 1, "text": "Krinkle Gauze Roll - Sterile (3161)"},
        {"index": 2, "text": "Suction Tubing Loop (43161)"},
    ]

    assert exact_suggestion("3161", offered) == offered[1]
    assert exact_suggestion("316", offered) is None
    assert exact_suggestion("3161", [{"index": 0, "text": "3161 pieces in a case (3553)"}]) is None
    assert exact_suggestion("3161", [{"index": 7, "text": "Gauze Code: 3161"}]) is not None
    # The code is not always the last thing in the line, but it is the
    # last thing in brackets: a description that names another part is
    # not this suggestion's own code.
    assert exact_suggestion("3161", [{"index": 3, "text": "Gauze (3161) $12.34 / CS"}]) is not None
    assert (
        exact_suggestion("3161", [{"index": 4, "text": "Refill for Krinkle (3161) (43161) $7.25"}])
        is None
    )


def test_the_saved_dynarex_cart_reads_but_refuses_to_be_added_to(tmp_path: Path) -> None:
    saved = DynarexFixtureCart(fixture_dir=Path("tests/fixtures/golden/data"))

    assert saved.read_cart().supplier == "dynarex"
    with pytest.raises(CartRefusal, match="saved copy of the cart"):
        saved.add_line("3161", 1)

    missing: SupplierCart = DynarexFixtureCart(fixture_dir=tmp_path)
    with pytest.raises(CartUnavailable, match="is missing"):
        missing.read_cart()
