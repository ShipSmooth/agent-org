"""Ask Zach's Dynarex portal what it is, and write nothing anywhere.

NAR turned out to have a real API, so staging there is an HTTP call whose
shape the catalogue itself confirms. Dynarex has no API anyone outside the
company can read about, so Shannon has to drive the portal — and driving a
portal is only safe if its actual mechanics are known rather than guessed
at. Three things have to be established before any staging code is worth
writing:

1. **Can it be reached from a machine at all?** One request from this
   project's build machine came back as Google's "Checking your browser"
   reCAPTCHA interstitial rather than the storefront; later requests from
   the same machine were served normally. So the challenge is occasional
   rather than constant, which is worse than a flat refusal: an unattended
   Monday-morning run would work most weeks and silently fail some.
2. **What the sign-in form and the cart page actually are** — the field
   names, the URLs, and whether the cart survives a fresh login in a clean
   browser profile the way it must for an unattended run.
3. **What a Quick Order row looks like**, since that is the mechanism the
   four Dynarex parts in boms.yaml — 3161, 3553, 3173, 3683 — would be
   staged through.

The first run of this script found no sign-in form, because it guessed
Magento's paths. The portal is commercebuild, not Magento: sign-in is
/user/login with fields named login_username and login_password, the cart
is /cart, Quick Order is /cart/quickorders, and search is /product_search.
Those are read off the live site rather than guessed at now — and every
path this script tries is printed with what was actually at it, so a miss
names the page it landed on instead of saying "not found".

This script answers all three **read-only**. It opens pages, reads the DOM,
and prints what it found. It types into no field except the sign-in form,
clicks no Add, no Quick Order submit, no checkout and no order button, and
refuses outright to navigate to any URL that looks like a checkout, order
or payment path.

    uv run python scripts/dynarex_portal_spike.py

It asks for the portal email and password at the prompt (nothing echoed,
nothing stored, nothing written to disk), or takes DYNAREX_EMAIL and
DYNAREX_PASSWORD from the environment. The password is never printed.
Output is filtered: anything that looks like an email address, a telephone
number or a street line is removed before it reaches the screen, so the
whole output can be pasted straight back.

A browser window opens and stays open; that is deliberate, so the cart on
screen can be compared with what the script prints. Add `--headless` to run
it without one.
"""

from __future__ import annotations

import os
import re
import sys
from getpass import getpass
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

# www.dynarex.com redirects here; using the apex directly keeps the printed
# URLs comparable with what a browser shows.
BASE = "https://dynarex.com"
PARTS = ("3161", "3553", "3173", "3683")

# Nothing here may be visited. The list is matched against the path of every
# URL this script is asked to open, and a match stops the script rather than
# skipping the page: a spike that quietly declined to look at something
# would be reported as "nothing there".
FORBIDDEN = re.compile(
    r"(checkout|onepage|placeorder|place-order|payment|paypal|purchase|submitorder|order/create)",
    re.IGNORECASE,
)

EMAILISH = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONEISH = re.compile(r"(?<!\d)(\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")
STREETISH = re.compile(
    r"\d+\s+[\w .]+\b(street|st|avenue|ave|road|rd|drive|dr|lane|ln|way"
    r"|suite|ste|boulevard|blvd)\b\.?",
    re.IGNORECASE,
)


class RefusedUrl(RuntimeError):
    """A URL that could place or pay for an order was asked for."""


def _clean(text: str) -> str:
    """Personal detail out; part numbers, quantities and prices in."""
    text = EMAILISH.sub("[redacted]", text)
    text = PHONEISH.sub("[redacted]", text)
    return STREETISH.sub("[redacted]", text)


def _say(label: str, value: Any) -> None:
    print(f"\n--- {label} ---")
    print(_clean(str(value))[:3000])


def _open(page: Page, url: str) -> None:
    path = urlparse(url).path or "/"
    if FORBIDDEN.search(path):
        raise RefusedUrl(url)
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)


def _credentials() -> tuple[str, str]:
    email = os.environ.get("DYNAREX_EMAIL") or input("dynarex.com email: ").strip()
    password = os.environ.get("DYNAREX_PASSWORD") or getpass("dynarex.com password (not echoed): ")
    return email, password


def _blocked_by_a_captcha(page: Page) -> bool:
    """The question the build machine could not answer for itself."""
    title = (page.title() or "").lower()
    if "recaptcha" in title or "checking your browser" in title:
        return True
    return page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha']").count() > 0


def _landed_on(page: Page, path: str) -> None:
    """What was actually at a path.

    The first version of this script printed only whether it found what it
    was looking for, so a wrong guess about the platform read as "the
    portal has no login page". Every attempt now says where it ended up.
    """
    try:
        text = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
    except PlaywrightTimeout:
        text = "(no body)"
    passwords = page.locator("input[type='password']").count()
    print(
        f"  tried {path}\n"
        f"    ended at {page.url}\n"
        f"    title: {page.title()!r}\n"
        f"    password fields: {passwords}, forms: {page.locator('form').count()}, "
        f"captcha: {_blocked_by_a_captcha(page)}\n"
        f"    first words: {_clean(text)[:200]!r}"
    )


def _describe_forms(page: Page, label: str) -> None:
    """Every form on the page, by the names its fields actually carry.

    Selectors guessed from how a storefront usually looks are the single
    most likely thing to break silently later, so they are read off the
    page here instead.
    """
    forms = page.evaluate(
        """() => [...document.querySelectorAll('form')].map(form => ({
            action: form.getAttribute('action'),
            method: (form.getAttribute('method') || 'get').toUpperCase(),
            id: form.id || null,
            fields: [...form.querySelectorAll('input, select, textarea')]
                .filter(field => field.type !== 'hidden')
                .map(field => ({
                    name: field.getAttribute('name'),
                    type: field.getAttribute('type') || field.tagName.toLowerCase(),
                    id: field.id || null,
                    placeholder: field.getAttribute('placeholder') || null,
                })),
            buttons: [...form.querySelectorAll('button, input[type=submit]')]
                .map(button => (button.innerText || button.value || '').trim())
                .filter(Boolean),
        }))"""
    )
    _say(f"forms on {label}", forms)


def _sign_in(page: Page, email: str, password: str) -> bool:
    print("\n--- looking for the sign-in page ---")
    for path in ("/user/login/", "/user/login", "/customer/account/login/", "/login"):
        _open(page, BASE + path)
        _landed_on(page, path)
        if _blocked_by_a_captcha(page):
            print(f"\nCAPTCHA at {path} — the portal is challenging this browser.")
            return False
        if page.locator("input[type='password']").count():
            break
    else:
        print("\nNo sign-in form found at any of the paths above.")
        return False

    print(f"\nsign-in page: {page.url}")
    _describe_forms(page, "the sign-in page")

    # commercebuild names these login_username and login_password; the
    # broader selectors are the fallback if the theme is ever changed.
    page.locator(
        "input[name='login_username'], input[type='email'], input[name*='email' i]"
    ).first.fill(email)
    page.locator("input[name='login_password'], input[type='password']").first.fill(password)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=45_000):
        page.locator("button[type='submit'], input[type='submit']").first.click()

    signed_in = page.locator("text=/sign out|log out|my account/i").count() > 0
    print(f"after sign-in: {page.url} — signed in: {signed_in}")
    if not signed_in:
        _say("what the page says instead", page.locator("body").inner_text()[:600])
    return signed_in


def _read_cart(page: Page) -> None:
    """Question 2: what does the cart look like, and is it Zach's cart?"""
    print("\n--- looking for the cart ---")
    for path in ("/cart", "/cart/index", "/checkout/cart/"):
        try:
            _open(page, BASE + path)
        except RefusedUrl:
            # A cart page whose URL says 'checkout' is still a checkout path
            # by name. The name wins: this script does not decide that a URL
            # containing 'checkout' is harmless.
            print(f"  refused to open {path} — it matches a checkout path by name.")
            continue
        _landed_on(page, path)
        if page.locator("text=/shopping cart|your cart|cart is empty/i").count():
            break
    print(f"\ncart page: {page.url}")
    rows = page.evaluate(
        """() => [...document.querySelectorAll('table tr, [class*=cart-item], [class*=line-item]')]
            .map(row => (row.innerText || '').replace(/\\s+/g, ' ').trim())
            .filter(text => text.length > 3)
            .slice(0, 40)"""
    )
    _say("what the cart page shows, row by row", rows)


def _quick_order(page: Page) -> None:
    """Question 3: what is a Quick Order row made of?"""
    print("\n--- looking for Quick Order ---")
    for path in ("/cart/quickorders", "/quickorder", "/quick-order"):
        _open(page, BASE + path)
        _landed_on(page, path)
        if page.locator("input[name='login_password']").count():
            print("    that is the sign-in page — Quick Order is behind the login.")
            continue
        body = page.locator("body").inner_text().lower()
        if (
            "quick order" in body
            or page.locator("input[name*='sku' i], input[name*='code' i]").count()
        ):
            print(f"\nquick order page: {page.url}")
            _describe_forms(page, "the quick order page")
            return
    print("\nNo Quick Order page found at any of the paths above.")


def _catalogue(page: Page) -> None:
    """Do the four parts in boms.yaml exist under those numbers?"""
    for part in PARTS:
        _open(page, f"{BASE}/product_search/?q={part}")
        results = page.evaluate(
            """() => [...document.querySelectorAll('[class*=product], [class*=item]')]
                .map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim())
                .filter(text => /Code:/.test(text) && text.length < 200)
                .slice(0, 8)"""
        )
        codes = sorted({code for text in results for code in re.findall(r"Code:\s*(\S+)", text)})
        # The site's search is a 'contains' search — 3161 also returns 33161
        # and 43161 — so an exact code is the only acceptable answer, the
        # same standard the NAR catalogue lookup was fixed to apply.
        print(f"  {part}: exact match {part in codes} — search returned {codes}")
        named = {
            _clean(text) for text in results if re.search(rf"Code:\s*{re.escape(part)}\b", text)
        }
        for text in sorted(named, key=len, reverse=True)[:2]:
            print(f"      {text}")


def main(argv: list[str]) -> int:
    print(__doc__.split("\n\n")[0])
    print(f"Reading {BASE} only. Nothing is added to the cart. Nothing is ordered.\n")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless="--headless" in argv)
        except PlaywrightError as missing:
            print(f"{missing}\n\nRun `uv run playwright install chromium` first.")
            return 1
        page = browser.new_page()
        try:
            _open(page, BASE + "/")
            _landed_on(page, "/")
            if _blocked_by_a_captcha(page):
                print(
                    "CAPTCHA on the front page. The portal is challenging this browser "
                    "before any login is attempted — which is the answer to question 1, "
                    "and it means an unattended run cannot sign in from here. Stopping."
                )
                return 1

            email, password = _credentials()
            if not _sign_in(page, email, password):
                print("\nNot signed in, so nothing further was read. Nothing was changed.")
                return 1

            _read_cart(page)
            _quick_order(page)
            print("\n--- do the four parts exist under those numbers? ---")
            _catalogue(page)
        except PlaywrightTimeout as timeout:
            print(f"\nTimed out: {timeout}")
            return 1
        finally:
            if "--headless" in argv:
                browser.close()

    print(
        "\nDone — nothing was written. Paste all of the above back, plus:\n"
        "  (a) did a captcha appear at any point, including before the script ran?\n"
        "  (b) does the cart printed above match what your own browser shows?\n"
        "  (c) is Quick Order how you actually order, or do you add from product pages?"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
