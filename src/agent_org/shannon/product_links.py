"""Where to click for a line Zach orders by hand.

Shannon stages nothing for Dynarex or Amazon Business: dynarex.com serves an
image CAPTCHA, which nobody is to automate past, and there is no Amazon
integration at all. Those lines are ordered by Zach himself, out of the
weekly email, so the email has to take him to the right product page.

The bar is exactness, not coverage. A search-results page could put a
neighbouring item first — Dynarex's own search for 3161 offers 33161 and
43161 — and a link Zach trusts is how the wrong thing lands in a real cart.
So a link is produced only from an identifier that names one product:

  * `product_url`, checked at config load against the item code (loader);
  * an Amazon purchase ASIN, which addresses exactly one listing.

Anything else returns None, and the report prints the item code and asks him
to find it himself. That is the intended outcome, not a gap to fill later.
"""

from __future__ import annotations

import re

from agent_org.config.models import Component

AMAZON_DP = "https://www.amazon.com/dp/"

# An ASIN is ten characters: modern ones start B0, and the older
# ISBN-shaped ones are all digits. Anything else is some other identifier
# wearing the field, and gets no link.
ASIN = re.compile(r"^(B0[0-9A-Z]{8}|[0-9]{9}[0-9X])$")


def amazon_product_url(asin: str | None) -> str | None:
    """`/dp/<ASIN>` addresses one listing; a malformed ASIN addresses none."""
    if asin is None:
        return None
    asin = asin.strip().upper()
    return f"{AMAZON_DP}{asin}" if ASIN.match(asin) else None


def product_url(component: Component) -> str | None:
    """The page for this exact component, or None to print it unlinked."""
    if component.product_url is not None:
        return component.product_url
    if component.supplier == "amazon_business":
        return amazon_product_url(component.purchase_asin)
    return None


__all__ = ["amazon_product_url", "product_url"]
