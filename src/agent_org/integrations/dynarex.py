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

# A Quick Order row: a text box asking for a code, and the quantity beside
# it. The search box is excluded by name and by the form it lives in — it
# is the field that made the spike search for an empty string.
QUICK_ORDER_JS = """() => {
  const fields = [...document.querySelectorAll("input[type=text], input:not([type])")];
  const wanted = /sku|code|item|part|product/i;
  for (const field of fields) {
    const name = (field.getAttribute('name') || '') + ' ' + (field.id || '') + ' ' +
                 (field.getAttribute('placeholder') || '');
    if (name.trim() === 'q' || field.id === 'search') continue;
    if (field.closest('#search_mini_form')) continue;
    if (!wanted.test(name)) continue;
    if (field.value) continue;
    const row = field.closest('tr, [class*=row], [class*=item], form, div');
    const qty = row && row.querySelector("input[name*='qty' i], input[id*='qty' i]");
    if (!qty) continue;
    field.setAttribute('data-shannon-sku', '1');
    qty.setAttribute('data-shannon-qty', '1');
    return {sku_field: field.getAttribute('name') || field.id,
            qty_field: qty.getAttribute('name') || qty.id};
  }
  return null;
}"""

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
            row = page.evaluate(QUICK_ORDER_JS)
            if row is None:
                raise CartUnavailable(
                    f"Nothing on {QUICK_ORDER_PATH} looks like a Quick Order row — no "
                    "empty part-number box with a quantity beside it. Nothing was "
                    f"added. The page holds these forms: {page.evaluate(FORMS_JS)}"
                )
            page.locator("[data-shannon-sku]").first.fill(sku)
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

    def _submit(self, page: Page) -> None:
        """Click the button that adds the row, and nothing that buys it."""
        form = page.locator("form:has([data-shannon-sku])").first
        buttons = form.locator("button, input[type=submit]") if form.count() else None
        if buttons is not None:
            for index in range(buttons.count()):
                button = buttons.nth(index)
                label = (button.inner_text() or button.get_attribute("value") or "").strip()
                if BUYING_BUTTON.search(label):
                    # Not a refusal of the whole run: the portal is allowed
                    # to put a checkout button on the page. It is a refusal
                    # to be the thing that clicks it.
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
