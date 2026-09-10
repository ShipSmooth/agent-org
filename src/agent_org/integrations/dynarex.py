"""Dynarex — the cart, driven through the portal in a browser.

NAR has a REST API, so a line there is an HTTP call whose reply names what
landed. Dynarex has nothing of the sort: dynarex.com is a commercebuild
portal, the cart lives behind an email-and-password login, and the way
Zach's four parts go in is the Quick Order page. So this client drives a
real browser, and everything it knows about the portal was read off the
live site rather than assumed:

    sign-in     /user/login          login_username / login_password
    cart        /cart                "Your cart is currently empty" when it is
    quick order /cart/quickorders    behind the login; redirects to it when out
    search      /product_search/?q=  a *contains* search, not an exact one

Two lessons from NAR are built in rather than re-learned:

* **A SKU is the part itself.** Searching Dynarex for 3161 also answers
  with 33161 and 43161, exactly as searching NAR for 30-0052 answered with
  the kits containing it. Only a product whose own code is the SKU counts,
  and a SKU with no exact match is refused rather than guessed at.
* **The form is chosen by what it contains, not by where it sits.** The
  login page carries the search box twice before it carries the login, and
  the spike spent a week submitting an empty search because it took the
  first form on the page. Every form used here is located by the field
  that identifies it.

Quick Order works like this, watched by hand in the browser rather than
inferred from markup:

1. The page opens with about twenty blank rows, each already holding a
   search box and a quantity box.
2. Typing in a search box drops an autocomplete under it. It is the
   contains-search again: 3161 offers 33161 and 43161 too.
3. Clicking a suggestion is what does everything. Typing alone does
   nothing; there is no "type and tab" path and no Add button.
4. About two seconds later the row fills in its description, price, UOM
   and a quantity of **1** — and the line is in the cart from that
   moment, before any quantity has been chosen.
5. The quantity box is then overwritten with the real quantity.

Step 4 is the dangerous one, and it shapes the error handling here: an
interruption between the click and the corrected quantity leaves the line
in the cart at 1 rather than leaving the cart clean. Every failure after
the click therefore says what the cart now holds and asks Zach to correct
it by hand — Shannon can read a cart and add to one, and cannot take a
line out of either.

Where the portal's markup is not known — the signed-in cart's rows, the
Quick Order row, the autocomplete — this client discovers it and
*refuses* when it cannot, quoting what it actually found. A cart that
cannot be read is unknown, never empty, and a Quick Order page that does
not look like one is not a page to start typing into.

Never checking out is enforced the same three ways as NAR, none of them a
setting: `ALLOWED_PATHS` is a closed set, `FORBIDDEN` catches a checkout
path added to it by a later edit, and nothing whose text offers to place
or pay for an order is ever clicked — which on this page means the
"Proceed to Checkout" button sitting beside the running sub-total.

There is no credential in this file. `DYNAREX_EMAIL` and
`DYNAREX_PASSWORD` are read from the environment under the entity's own
prefix at the moment of use, and never logged.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from agent_org.integrations.carts import (
    Cart,
    CartLine,
    CartRefusal,
    CartUnavailable,
    SavedCartCopy,
    money,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from playwright.sync_api import Page

DYNAREX_BASE_URL = "https://dynarex.com"
SUPPLIER = "dynarex"

LOGIN_PATH = "/user/login"
CART_PATH = "/cart"
QUICK_ORDER_PATH = "/cart/quickorders"
SEARCH_PATH = "/product_search/"

# Every path this client may open. Staging needs four; a fifth is a visible
# line in a diff that a reviewer can refuse.
ALLOWED_PATHS = frozenset({LOGIN_PATH, CART_PATH, QUICK_ORDER_PATH, SEARCH_PATH})

# Belt as well as braces, and deliberately not `order` on its own: the
# Quick Order page is /cart/quickorders, and a rule that refused it would
# have to be loosened by whoever hit it, which is how a safety rule dies.
FORBIDDEN = re.compile(
    r"checkout|onepage|payment|paypal|purchase|place[-_ ]?order|submit[-_ ]?order"
    r"|/order/|/orders/|billing",
    re.IGNORECASE,
)

# The same words on anything clickable. The Quick Order page carries
# "Proceed to Checkout" and a running sub-total; the only thing Shannon
# clicks there is an autocomplete suggestion, and it is checked against
# this first — a suggestion is not a button, but the rule is about what
# the click might do, not about the tag it is on.
BUYING_BUTTON = re.compile(
    r"check\s*out|place\s*(the\s*)?order|submit\s*order|pay\b|paypal|purchase|buy\s*now",
    re.IGNORECASE,
)

EMAIL_VAR = "DYNAREX_EMAIL"
PASSWORD_VAR = "DYNAREX_PASSWORD"

EMPTY_CART = re.compile(r"cart is (currently )?empty", re.IGNORECASE)
CODE_IN_TEXT = re.compile(
    r"(?:code|item(?:\s*#)?|sku)\s*[:#]\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE
)
# How the autocomplete writes a part number: "Krinkle Gauze Roll (3161)".
# Not anchored to the end of the line: a suggestion carries a price after
# the code as often as not.
SUGGESTED_CODE = re.compile(r"\(([A-Za-z0-9][\w.-]*)\)")
GRAND_TOTAL = re.compile(r"(?:grand\s+total|order\s+total|total)\D{0,20}\$\s*([\d,]+\.\d{2})", re.I)
SIGNED_IN = re.compile(r"sign\s*out|log\s*out|my account", re.IGNORECASE)


def credentials(credentials_prefix: str = "") -> tuple[str, str]:
    """The Dynarex login, from the environment and nowhere else."""
    names = (f"{credentials_prefix}{EMAIL_VAR}", f"{credentials_prefix}{PASSWORD_VAR}")
    email, password = (os.environ.get(name, "").strip() for name in names)
    if not email or not password:
        missing = " and ".join(
            name for name, value in zip(names, (email, password), strict=True) if not value
        )
        raise CartUnavailable(
            f"{missing} is not set, so the dynarex.com cart cannot be read. Nothing "
            "has been staged. Put the login in the environment (or in .env) and run "
            "again."
        )
    return email, password


def refuse_unless_safe(path: str) -> None:
    """The gate every page load goes through, before the browser moves."""
    if FORBIDDEN.search(path):
        raise CartRefusal(
            f"'{path}' is a checkout, order or payment path. Shannon stages a cart "
            "and stops there — permanently, at every tier."
        )
    if path.rstrip("/") not in {allowed.rstrip("/") for allowed in ALLOWED_PATHS}:
        raise CartRefusal(
            f"'{path}' is not one of the pages Shannon may open on dynarex.com. "
            "Nothing was opened and nothing was staged."
        )


# The rows of the cart, as the page has them. Written as a script rather
# than a chain of selectors because what a commercebuild cart row is made
# of is not known from outside the login, so this collects candidates and
# Python decides which are real.
CART_ROWS_JS = """() => {
  const rows = new Set();
  for (const input of document.querySelectorAll("input[name*='qty' i], input[id*='qty' i]")) {
    const row = input.closest('tr, [class*=item], [class*=row], li');
    if (row) rows.add(row);
  }
  return [...rows].map(row => ({
    text: (row.innerText || '').replace(/\\s+/g, ' ').trim(),
    quantity: (row.querySelector("input[name*='qty' i], input[id*='qty' i]") || {}).value || '',
    sku: row.getAttribute('data-sku') || row.getAttribute('data-product-code') || '',
  })).filter(row => row.text.length > 2);
}"""

# The pieces of the page every scan below shares. The part-number box on
# the live Quick Order page sits in a cell whose own class is `search-box`
# — the word means something else there — so the site's search box is
# excluded by the form it belongs to and by its own name, never by the
# word 'search' appearing somewhere near it.
QUICK_ORDER_PRELUDE = """
  const SITE_SEARCH = "#search_mini_form, form[action*='product_search' i], " +
                      "form[action*='/search' i]";
  const QTY = "input[name*='qty' i], input[id*='qty' i], input[class*='qty' i], " +
              "input[name*='quantity' i], input[id*='quantity' i], " +
              "input[aria-label*='quantity' i], input[type=number]";
  const CODEISH = /sku|code|item|part|product|catalog/i;
  const shown = node => !!(node.offsetParent || node.getClientRects().length);
  const named = field => [field.getAttribute('name'), field.id,
                          field.getAttribute('placeholder'),
                          field.getAttribute('aria-label')]
      .filter(Boolean).join(' ');
  const described = field => named(field) + ' ' + (field.className || '');
  const isSiteSearch = field => (field.getAttribute('name') || '').trim() === 'q' ||
      field.id === 'search' || !!field.closest(SITE_SEARCH);
  const typed = field => (field.getAttribute('type') || 'text').toLowerCase();
  const boxes = () => [...document.querySelectorAll('input')]
      .filter(field => ['text', 'search', 'tel', ''].includes(typed(field)))
      .filter(shown)
      .filter(field => !field.disabled && !field.readOnly)
      .filter(field => !isSiteSearch(field));
  // The part-number box and its quantity sit in different cells of the
  // same row, so the enclosing element is climbed to rather than taken:
  // `closest('div')` is the cell, and the cell holds no quantity.
  const beside = (field, selector) => {
    for (let node = field.parentElement, step = 0;
         node && node !== document.body && step < 8;
         node = node.parentElement, step++) {
      const found = [...node.querySelectorAll(selector)].filter(shown);
      if (found.length) return found[0];
    }
    return null;
  };
"""

# A blank row: a search box with nothing typed in it and a quantity box
# beside it. Both are there from page load — the page opens with about
# twenty of these — so a row is a pair, and a box with no quantity beside
# it is not a row however it is named.
QUICK_ORDER_ROW_JS = (
    """() => {"""
    + QUICK_ORDER_PRELUDE
    + """
  const empty = boxes().filter(field => !field.value);
  const named_first = empty.filter(box => CODEISH.test(described(box)))
      .concat(empty);
  for (const box of named_first) {
    const qty = beside(box, QTY);
    if (!qty) continue;
    box.setAttribute('data-shannon-sku', '1');
    qty.setAttribute('data-shannon-qty', '1');
    return {sku_field: named(box).trim() || '(unnamed)',
            qty_field: named(qty).trim() || '(unnamed)',
            blank_rows: empty.length};
  }
  return null;
}"""
)

# The autocomplete the portal drops under the row as the code is typed.
# Its markup is not known from outside the login, so the lists are found
# and then split into rows, and Python decides which — if any — is the
# part itself.
#
# A row is *not* the innermost element carrying the typed digits. The
# portal highlights the match as its own span, so 33161 is written
# `3<span>3161</span>`, and reading the innermost node reads 3161 — the
# spike came back with ['3161', '3161', '3161'] for three different
# products. Descending stops at the row: a node is split only when two or
# more of its children carry the digits (a list of suggestions) or when
# its one matching child is the whole of it (a wrapper).
SUGGESTIONS_JS = (
    """(wanted) => {"""
    + QUICK_ORDER_PRELUDE
    + """
  const box = document.querySelector('[data-shannon-sku]');
  if (!box) return [];
  const want = wanted.toLowerCase();
  const words = node => (node.innerText || '').replace(/\\s+/g, ' ').trim();
  const holds = node => shown(node) && words(node).toLowerCase().includes(want);
  const LIST = "ul, ol, table, [role=listbox], [class*='autocomplete' i], " +
               "[class*='suggest' i], [class*='typeahead' i], " +
               "[class*='dropdown' i], [class*='result' i], li, [role=option]";
  const lists = [...document.querySelectorAll(LIST)]
      .filter(holds)
      .filter(node => !node.contains(box) && node !== box)
      .filter(node => !node.querySelector('input, form'));
  const outermost = lists.filter(
      node => !lists.some(other => other !== node && other.contains(node)));
  const rows = [];
  const split = (node, depth) => {
    const children = [...node.children].filter(holds);
    if (depth < 8 && children.length > 1) {
      children.forEach(child => split(child, depth + 1));
    } else if (depth < 8 && children.length === 1 &&
               words(children[0]) === words(node)) {
      split(children[0], depth + 1);
    } else {
      rows.push(node);
    }
  };
  outermost.forEach(node => split(node, 0));
  const seen = new Set();
  const options = [];
  for (const node of rows) {
    const text = words(node);
    if (text.length < 3 || text.length > 200 || seen.has(text)) continue;
    seen.add(text);
    node.setAttribute('data-shannon-option', String(options.length));
    options.push({index: options.length, text: text});
    if (options.length >= 40) break;
  }
  return options;
}"""
)

# Whether the portal has answered the click yet: the row fills its own
# description, price and quantity in about two seconds, and the line is
# in the cart from that moment.
ROW_FILLED_JS = """() => {
  const qty = document.querySelector('[data-shannon-qty]');
  if (!qty || !String(qty.value).trim()) return null;
  const row = qty.closest('tr, [class*=row i], div');
  return {quantity: String(qty.value).trim(),
          row: ((row || qty).innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200)};
}"""

# What is actually on the page, for a refusal to quote. Forms alone were
# not enough: the live page answered with its two search forms and nothing
# else, which says a Quick Order row is not inside a form there but says
# nothing at all about the boxes that are on it.
FIELDS_JS = (
    """() => {"""
    + QUICK_ORDER_PRELUDE
    + """
  return [...document.querySelectorAll('input, select, textarea')]
      .filter(field => (field.getAttribute('type') || '').toLowerCase() !== 'hidden')
      .slice(0, 40)
      .map(field => ({
        tag: field.tagName.toLowerCase(),
        type: field.getAttribute('type') || '',
        name: field.getAttribute('name') || field.id || null,
        placeholder: field.getAttribute('placeholder') || null,
        cell: (field.parentElement || {}).className || null,
        shown: shown(field),
      }));
}"""
)

FORMS_JS = """() => [...document.querySelectorAll('form')].map(form => ({
  action: form.getAttribute('action'),
  fields: [...form.querySelectorAll('input, select')]
      .filter(field => field.type !== 'hidden')
      .map(field => field.getAttribute('name') || field.id || field.type),
}))"""


def cart_lines(rows: list[dict[str, Any]], supplier: str = SUPPLIER) -> tuple[CartLine, ...]:
    """The cart's rows as lines, or a refusal naming the row that beat us.

    A row whose part number cannot be read is not skipped. Skipping it
    would make the line invisible to the verification that runs after an
    add, and an invisible line is how a cart quietly ends up holding twice
    what it should.
    """
    lines: list[CartLine] = []
    for row in rows:
        text = str(row.get("text", ""))
        sku = str(row.get("sku") or "")
        if not sku:
            found = CODE_IN_TEXT.search(text)
            sku = found.group(1) if found else ""
        if not sku:
            raise CartUnavailable(
                f"A line in the {supplier} cart could not be read: no part number "
                f"anywhere in '{text[:120]}'. The cart is not reported as empty and "
                "nothing was staged — an unreadable cart is unknown, never empty."
            )
        quantity = str(row.get("quantity", "")).strip()
        if not quantity.isdigit():
            raise CartUnavailable(
                f"The {supplier} cart shows {sku} with a quantity of '{quantity}', "
                "which is not a number. Nothing was staged."
            )
        price = re.search(r"\$\s*([\d,]+\.\d{2})", text)
        lines.append(
            CartLine(
                sku=sku,
                name=_name_in(text, sku),
                quantity=int(quantity),
                price=money(price.group(1).replace(",", "")) if price else None,
            )
        )
    return tuple(lines)


def _name_in(text: str, sku: str) -> str:
    """The product's name out of a row of text, best effort and no more."""
    before = text.split(sku)[0]
    cleaned = re.sub(r"(?:code|item(?:\s*#)?|sku)\s*[:#]\s*$", "", before, flags=re.IGNORECASE)
    return cleaned.strip(" -|·").strip()[:120]


def exact_suggestion(sku: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The dropdown entry that *is* this part, out of everything offered.

    The Quick Order autocomplete is the contains-search again, in a
    smaller box: typing 3161 offers 33161 and 43161 too, and clicking one
    of those puts the wrong product in the cart with no further warning.
    A suggestion counts only if it names exactly one code — in a
    `(3161)` or after a `Code:` — and that code is the part itself. One,
    because a line reading "replaces (3161)" before its own `(43161)`
    offers no way to tell a description from a part number, and the
    wrong guess puts the wrong product in a real cart. A line Shannon
    cannot read unambiguously is left for Zach to add by hand.
    """
    for option in options:
        text = str(option.get("text", ""))
        codes = set(CODE_IN_TEXT.findall(text)) | set(SUGGESTED_CODE.findall(text))
        if codes == {sku}:
            return option
    return None


def exact_code(sku: str, results: list[str]) -> str | None:
    """The result that *is* this part, out of everything the search returned.

    Dynarex's search is a contains search: 3161 answers with 33161 and
    43161 as well, which are real products with real names that are simply
    not the part being ordered. The same standard the NAR lookup was fixed
    to apply: an exact code or nothing.
    """
    for text in results:
        for code in CODE_IN_TEXT.findall(text):
            if code == sku:
                return text
    return None


@dataclass
class DynarexPortalCart:
    """Read the dynarex.com cart, and add a line to it through Quick Order.

    One browser for the whole run: signing in costs a page load and the
    portal's session is a cookie, so the first read pays for it and every
    line after that is two page loads.
    """

    supplier: str = SUPPLIER
    credentials_prefix: str = ""
    base_url: str = DYNAREX_BASE_URL
    headless: bool = True
    timeout_ms: int = 45_000
    # How long a Quick Order row is given to appear. The row is drawn and
    # then filled in by the portal's own JavaScript, so 'not there' only
    # means anything after waiting for it.
    settle_ms: int = 15_000
    # Injected by the tests, which drive every path below against a local
    # portal, with no account and nothing of Zach's touched.
    page: Page | None = field(default=None, compare=False, repr=False)
    _signed_in: bool = field(default=False, init=False, compare=False, repr=False)

    def read_cart(self) -> Cart:
        with self._portal() as page:
            return self._read(page)

    def _read(self, page: Page) -> Cart:
        """The cart, on a page that is already signed in."""
        self._open(page, CART_PATH)
        body = self._text(page)
        if EMPTY_CART.search(body):
            return Cart(supplier=self.supplier, cart_id=CART_PATH, lines=())
        rows = list(page.evaluate(CART_ROWS_JS))
        if not rows:
            raise CartUnavailable(
                "The dynarex.com cart page says neither that the cart is empty "
                "nor what is in it, so what the cart holds is unknown. Nothing "
                f"was staged. The page began: '{body[:160]}'"
            )
        total = GRAND_TOTAL.search(body)
        return Cart(
            supplier=self.supplier,
            cart_id=CART_PATH,
            lines=cart_lines(rows, self.supplier),
            grand_total=money(total.group(1).replace(",", "")) if total else None,
        )

    def add_line(self, sku: str, quantity: int) -> CartLine:
        """Put one line in the cart through Quick Order, and read it back.

        The part number is confirmed against the catalogue first — an
        exact code, or nothing goes in the box — because a contains search
        means a typo does not fail, it orders something else.
        """
        if quantity <= 0:
            raise CartRefusal(f"Asked to add {quantity} of {sku}, which is not a quantity.")
        with self._portal() as page:
            name = self._confirm(page, sku)
            held_before = self._read(page).quantity_of(sku)
            self._open(page, QUICK_ORDER_PATH)
            self._quick_order_row(page, sku)
            # Everything from here on can leave the line in the cart at the
            # portal's default of 1, so a failure has to say so rather than
            # read as "nothing was added".
            self._choose(page, sku)
            self._set_quantity(page, quantity)
            landed = self._read(page)

        held_after = landed.quantity_of(sku)
        if held_after != held_before + quantity:
            raise CartUnavailable(
                f"{quantity} of {sku} was entered on Quick Order and the cart "
                f"afterwards holds {held_after} of it where {held_before + quantity} "
                f"was expected. {self._by_hand(sku, held_after, held_before + quantity)}"
            )
        for line in landed.lines:
            if line.sku == sku:
                return CartLine(
                    sku=sku, name=line.name or name, quantity=quantity, price=line.price
                )
        raise CartUnavailable(  # pragma: no cover - unreachable while the count agrees
            f"{sku} is not in the dynarex.com cart after adding it. Nothing is reported as staged."
        )

    def _quick_order_row(self, page: Page, sku: str) -> None:
        """Take a blank row: an empty search box with a quantity beside it.

        Nothing is typed here, so nothing can land in the cart yet. The
        page opens with about twenty blank rows and both boxes of each are
        there from the start, so a search box with no quantity anywhere
        near it is some other box and is left alone.
        """
        if self._wait_for(page, QUICK_ORDER_ROW_JS) is None:
            raise CartUnavailable(
                f"No blank Quick Order row on {QUICK_ORDER_PATH} — no empty search "
                f"box with a quantity beside it, after {self.settle_ms // 1000}s. "
                f"{sku} was not typed and nothing was added. The page holds these "
                f"fields: {page.evaluate(FIELDS_JS)} and these forms: "
                f"{page.evaluate(FORMS_JS)}"
            )

    def _choose(self, page: Page, sku: str) -> None:
        """Type the part number and click the suggestion that *is* the part.

        Typing alone does nothing on this page: the row is filled in by
        clicking an entry in the autocomplete, and the portal puts the
        line in the cart at quantity 1 the moment that entry is clicked.
        So the click is the committing act, and it is only made against a
        suggestion whose own code is the SKU — the dropdown offers 33161
        and 43161 to someone typing 3161, and they are real products.
        """
        box = page.locator("[data-shannon-sku]").first
        box.click()
        box.press_sequentially(sku, delay=60, timeout=self.timeout_ms)

        offered: list[dict[str, Any]] = []
        deadline = monotonic() + self.settle_ms / 1000
        while True:
            offered = [dict(option) for option in page.evaluate(SUGGESTIONS_JS, sku)]
            wanted = exact_suggestion(sku, offered)
            if wanted is not None:
                break
            if monotonic() >= deadline:
                raise CartUnavailable(
                    f"The Quick Order autocomplete never offered {sku} itself within "
                    f"{self.settle_ms // 1000}s of it being typed. Nothing was clicked "
                    "and nothing was added — a line only goes in when a suggestion is "
                    f"clicked. It offered: {[option['text'] for option in offered][:10]}"
                )
            page.wait_for_timeout(250)

        if BUYING_BUTTON.search(str(wanted["text"])):
            raise CartRefusal(
                f"The Quick Order entry offered for {sku} reads "
                f"'{wanted['text'][:80]}', which offers to check out or pay rather "
                "than to name a product. It was not clicked and nothing was added."
            )
        page.locator(f"[data-shannon-option='{wanted['index']}']").first.click()
        # The portal takes a couple of seconds to answer the click with the
        # description, price and a quantity of 1. The line is in the cart
        # from that answer, not from the button that does not exist.
        if self._wait_for(page, ROW_FILLED_JS) is None:
            raise CartUnavailable(
                f"{sku} was chosen from the Quick Order dropdown and the row never "
                f"filled itself in within {self.settle_ms // 1000}s. The portal adds "
                "the line the moment a suggestion is clicked, so the cart may now hold "
                f"1 of {sku}. {self._by_hand(sku, None, None)}"
            )

    def _set_quantity(self, page: Page, quantity: int) -> None:
        """Correct the quantity the portal defaulted to 1.

        There is no Add button to press: the line is already in the cart,
        and this edits it. The box is left with a change event and a Tab,
        because the portal updates the cart off the field losing focus.
        """
        qty = page.locator("[data-shannon-qty]").first
        qty.click()
        qty.fill(str(quantity))
        qty.press("Tab")
        page.wait_for_timeout(1_000)

    def _wait_for(self, page: Page, script: str) -> dict[str, Any] | None:
        """Run a scan until it finds something, or until time is up.

        The portal answers in its own time, so a scan is a question asked
        repeatedly rather than once.
        """
        deadline = monotonic() + self.settle_ms / 1000
        while True:
            found = page.evaluate(script)
            if found is not None:
                return dict(found)
            if monotonic() >= deadline:
                return None
            page.wait_for_timeout(250)

    def _by_hand(self, sku: str, holding: int | None, wanted: int | None) -> str:
        """What Zach has to do about a half-finished line, in one sentence.

        Shannon cannot take a line out of a Dynarex cart — she can read one
        and add to one, and that is the whole of her authority here. The
        thing she owes him instead is an exact statement of what is in the
        cart, because this page adds at quantity 1 before the quantity is
        corrected: an interruption leaves the line there and wrong, not
        absent, and "the add failed" would be read as "the cart is clean".
        """
        held = "an unknown quantity" if holding is None else str(holding)
        expected = "" if wanted is None else f" (it should be {wanted})"
        return (
            f"The dynarex.com cart now holds {held} of {sku}{expected}. Shannon does "
            "not remove or edit cart lines, so correct it by hand on "
            f"{self.base_url}{CART_PATH} before ordering anything. Nothing has been "
            "checked out."
        )

    def _confirm(self, page: Page, sku: str) -> str:
        """What dynarex.com calls this part, or a refusal to add it at all."""
        self._open(page, f"{SEARCH_PATH}?q={sku}")
        results = [
            str(text)
            for text in page.evaluate(
                """() => [...document.querySelectorAll('[class*=product], [class*=item]')]
                    .map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim())
                    .filter(text => /code\\s*:/i.test(text) && text.length < 300)"""
            )
        ]
        found = exact_code(sku, results)
        if found is None:
            offered = sorted({code for text in results for code in CODE_IN_TEXT.findall(text)})
            raise CartRefusal(
                f"dynarex.com has no product whose code is {sku}. Its search offered "
                f"{', '.join(offered) or 'nothing'}, which are other products that "
                "contain those digits rather than the part itself. Nothing was added "
                "— check the part number."
            )
        return _name_in(found, sku)

    @contextmanager
    def _portal(self) -> Iterator[Page]:
        """A signed-in page, opening a browser only if one was not given."""
        if self.page is not None:
            self._sign_in(self.page)
            yield self.page
            return
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            try:
                self.page = browser.new_page()
                self._sign_in(self.page)
                yield self.page
            finally:
                self.page = None
                self._signed_in = False
                browser.close()

    def _sign_in(self, page: Page) -> None:
        if self._signed_in:
            return
        email, password = credentials(self.credentials_prefix)
        self._open(page, LOGIN_PATH)
        # The form that holds the password, never the first form on the
        # page: this page carries the search box twice before the login.
        form = page.locator("form:has(input[type='password'])").first
        if not form.count():
            raise CartUnavailable(
                f"No form on dynarex.com{LOGIN_PATH} holds a password field, so "
                "there was nothing to sign in with. Nothing was staged."
            )
        form.locator("input[name='login_username'], input[type='text']").first.fill(email)
        field = form.locator("input[type='password']").first
        field.fill(password)
        submit = form.locator("button[type='submit'], input[type='submit'], button:not([type])")
        if submit.count():
            submit.first.click()
        else:
            field.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        if not SIGNED_IN.search(self._text(page)):
            # The portal's own words, never the password: "you need to
            # verify your email address" and "invalid login" are the same
            # blank failure otherwise, and one of them is not a bad login.
            raise CartUnavailable(
                f"dynarex.com did not sign Shannon in: {self._complaint(page)}. Nothing "
                "was read and nothing was staged."
            )
        self._signed_in = True

    def _complaint(self, page: Page) -> str:
        said = page.locator("[class*='error' i], [class*='message' i], [role='alert']")
        for index in range(min(said.count(), 6)):
            words = re.sub(r"\s+", " ", said.nth(index).inner_text()).strip()
            words = re.sub(r"\s*Close Message\s*$", "", words)
            if words:
                return words[:200]
        return "it gave no reason"

    def _open(self, page: Page, path: str) -> None:
        refuse_unless_safe(urlparse(path).path)
        page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded", timeout=self.timeout_ms)
        if self._captcha(page):
            raise CartUnavailable(
                f"dynarex.com answered {path} with a captcha rather than the page. "
                "Nothing was read and nothing was staged — a challenge is not "
                "something an unattended run can answer, and it must not be "
                "worked around."
            )

    @staticmethod
    def _captcha(page: Page) -> bool:
        title = (page.title() or "").lower()
        if "recaptcha" in title or "checking your browser" in title:
            return True
        return page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha']").count() > 0

    @staticmethod
    def _text(page: Page) -> str:
        return re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()


@dataclass
class DynarexFixtureCart(SavedCartCopy):
    """The Dynarex cart as a saved JSON file, for a dry run with no login."""

    supplier: str = SUPPLIER
    filename: str = "dynarex_cart.json"


__all__ = [
    "ALLOWED_PATHS",
    "CART_PATH",
    "CART_ROWS_JS",
    "DYNAREX_BASE_URL",
    "FORBIDDEN",
    "LOGIN_PATH",
    "QUICK_ORDER_PATH",
    "SEARCH_PATH",
    "SUPPLIER",
    "DynarexFixtureCart",
    "DynarexPortalCart",
    "cart_lines",
    "credentials",
    "exact_code",
    "refuse_unless_safe",
]
