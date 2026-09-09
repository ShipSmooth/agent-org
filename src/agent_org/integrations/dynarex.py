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

Where the portal's markup is not known — the signed-in cart's rows, the
Quick Order row — this client discovers it and *refuses* when it cannot,
quoting what it actually found. A cart that cannot be read is unknown,
never empty, and a Quick Order page that does not look like one is not a
page to start typing into.

Never checking out is enforced the same three ways as NAR, none of them a
setting: `ALLOWED_PATHS` is a closed set, `FORBIDDEN` catches a checkout
path added to it by a later edit, and no button whose text offers to place
or pay for an order is ever clicked — the click is refused even if the
portal puts one inside the Quick Order form.

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

# The same words on a button. Quick Order's own button says "Add to Cart";
# anything offering to complete the purchase is not clicked, wherever the
# portal has chosen to put it.
BUYING_BUTTON = re.compile(
    r"check\s*out|place\s*(the\s*)?order|submit\s*order|pay\b|paypal|purchase|buy\s*now",
    re.IGNORECASE,
)

# The button that puts the row in the cart, by what it says. Anything the
# portal has not labelled as adding — Clear, Remove, Save, Upload — is
# left alone rather than clicked to see what it does.
ADDING_BUTTON = re.compile(r"add\b|add\s*to\s*cart|update\s*cart|submit\b", re.IGNORECASE)

EMAIL_VAR = "DYNAREX_EMAIL"
PASSWORD_VAR = "DYNAREX_PASSWORD"

EMPTY_CART = re.compile(r"cart is (currently )?empty", re.IGNORECASE)
CODE_IN_TEXT = re.compile(
    r"(?:code|item(?:\s*#)?|sku)\s*[:#]\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE
)
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

# The part-number box, wanted three ways in order of how much the page
# has said about it: called a code, or sitting beside a quantity, or the
# one box on the page that is not the site's search. The third is not a
# guess so much as the absence of an alternative — and it is only taken
# when there is exactly one, so it can never pick the wrong box.
QUICK_ORDER_SKU_JS = (
    """() => {"""
    + QUICK_ORDER_PRELUDE
    + """
  const empty = boxes().filter(field => !field.value);
  const ways = [
    ['it is named for a part number', empty.find(box => CODEISH.test(described(box)))],
    ['it has a quantity beside it', empty.find(box => beside(box, QTY))],
    ['it is the only box on the page', empty.length === 1 ? empty[0] : null],
  ];
  const chosen = ways.find(way => way[1]);
  if (!chosen) return null;
  const field = chosen[1];
  field.setAttribute('data-shannon-sku', '1');
  return {sku_field: named(field).trim() || '(unnamed)', found_because: chosen[0]};
}"""
)

# The quantity, looked for only once the part number is in the box: the
# live page fills a row in by AJAX as the code is typed, so a quantity
# that is not there at first is not a quantity that is not there.
QUICK_ORDER_QTY_JS = (
    """() => {"""
    + QUICK_ORDER_PRELUDE
    + """
  const field = document.querySelector('[data-shannon-sku]');
  if (!field) return null;
  const qty = beside(field, QTY);
  if (!qty) return null;
  qty.setAttribute('data-shannon-qty', '1');
  return {qty_field: named(qty).trim() || '(unnamed)'};
}"""
)

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
            page.locator("[data-shannon-qty]").first.fill(str(quantity))
            self._submit(page)
            landed = self._read(page)

        held_after = landed.quantity_of(sku)
        if held_after != held_before + quantity:
            raise CartUnavailable(
                f"{quantity} of {sku} was entered on Quick Order and the cart "
                f"afterwards holds {held_after} of it where {held_before + quantity} "
                "was expected. Check the cart on dynarex.com before ordering anything."
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
        """Put the part number in the row's box, and find its quantity.

        In two steps rather than one because the row is only half there
        when the page arrives: the box is typed into, the portal answers
        by AJAX, and the rest of the row — the quantity among it — comes
        back with that answer. A scan that ran once, the moment
        domcontentloaded fired, saw the first half and called the page
        empty of Quick Order rows.
        """
        found = self._wait_for(page, QUICK_ORDER_SKU_JS)
        if found is None:
            raise CartUnavailable(
                f"Nothing on {QUICK_ORDER_PATH} looks like a Quick Order row — no "
                "empty part-number box, named as one or with a quantity beside it. "
                f"Nothing was added. The page holds these fields: {page.evaluate(FIELDS_JS)} "
                f"and these forms: {page.evaluate(FORMS_JS)}"
            )
        # Typed rather than filled: the portal hangs its lookup off the
        # keystrokes, and a value set in one go arrives at a page that
        # never asked what the part number was.
        box = page.locator("[data-shannon-sku]").first
        box.click()
        box.press_sequentially(sku, delay=60, timeout=self.timeout_ms)
        if self._wait_for(page, QUICK_ORDER_QTY_JS) is None:
            raise CartUnavailable(
                f"The part-number box on {QUICK_ORDER_PATH} took {sku} (into "
                f"'{found['sku_field']}') and no quantity appeared beside it within "
                f"{self.settle_ms // 1000}s. Nothing was added. The page holds these "
                f"fields: {page.evaluate(FIELDS_JS)}"
            )

    def _wait_for(self, page: Page, script: str) -> dict[str, Any] | None:
        """Run a scan until it finds something, or until time is up.

        The portal renders a Quick Order row in its own time, so a scan
        is a question asked repeatedly rather than once.
        """
        deadline = monotonic() + self.settle_ms / 1000
        while True:
            found = page.evaluate(script)
            if found is not None:
                return dict(found)
            if monotonic() >= deadline:
                return None
            page.wait_for_timeout(250)

    def _submit(self, page: Page) -> None:
        """Click the button that adds the row, and nothing that buys it.

        The form is only the first place looked, not the only one: a row
        on the live page need not be inside a form at all — the portal
        adds by AJAX, and a page whose only forms are its two search
        boxes still has an Add button in the row. So the row itself, and
        then the page, are searched after it.
        """
        row = page.locator("[data-shannon-sku]").locator(
            "xpath=ancestor::*[.//*[@data-shannon-qty]][1]"
        )
        form = page.locator("form:has([data-shannon-sku])").first
        for scope in (form, row, page.locator("body")):
            if not scope.count():
                continue
            buttons = scope.locator("button, input[type=submit], input[type=button]")
            for index in range(buttons.count()):
                button = buttons.nth(index)
                label = (button.inner_text() or button.get_attribute("value") or "").strip()
                if BUYING_BUTTON.search(label):
                    # Not a refusal of the whole run: the portal is allowed
                    # to put a checkout button on the page. It is a refusal
                    # to be the thing that clicks it.
                    continue
                if not ADDING_BUTTON.search(label):
                    continue
                button.click()
                page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
                return
        page.locator("[data-shannon-qty]").first.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)

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
