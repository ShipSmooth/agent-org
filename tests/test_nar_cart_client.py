"""The NAR cart client, and the things it must refuse.

The refusals matter more than the happy path: they are the mechanism
behind "Shannon never checks out", and a mechanism nobody tests is a
comment.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agent_org.integrations.carts import CartRefusal, CartUnavailable
from agent_org.integrations.nar import (
    CART_PATH,
    ITEMS_PATH,
    TOTALS_PATH,
    NarCartClient,
    NarFixtureCart,
)

TOKEN = "not-a-real-token"


def _transport(seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path.endswith("/customer/token"):
            return httpx.Response(200, json=TOKEN)
        if path == CART_PATH:
            return httpx.Response(
                200,
                json={
                    "id": 4711,
                    "items": [{"item_id": 9, "sku": "30-0002", "name": "C-A-T", "qty": 4}],
                },
            )
        if path == TOTALS_PATH:
            return httpx.Response(200, json={"grand_total": 111.96, "quote_currency_code": "USD"})
        if path == ITEMS_PATH:
            body = json.loads(request.content)["cartItem"]
            return httpx.Response(
                200,
                json={
                    "item_id": 12,
                    "sku": body["sku"],
                    "name": "IPOK",
                    "qty": body["qty"],
                    "price": 57.99,
                },
            )
        raise AssertionError(f"unexpected path {path}")

    return httpx.MockTransport(handle)


def _client(**kwargs: object) -> NarCartClient:
    return NarCartClient(transport=_transport(), **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAR_EMAIL", "someone@example.invalid")
    monkeypatch.setenv("NAR_PASSWORD", "local-test-only")


def test_reads_the_cart_it_is_given(login: None) -> None:
    cart = _client().read_cart()
    assert cart.cart_id == "4711"
    assert cart.quantity_of("30-0002") == 4
    assert str(cart.grand_total) == "111.96"


def test_adds_a_line_and_reports_what_the_cart_says(login: None) -> None:
    line = _client().add_line("80-0167", 3)
    # The cart's own answer, not the SKU we asked for: for a configurable
    # product they can differ, and only the cart's is true.
    assert (line.sku, line.quantity) == ("80-0167", 3)


def test_refuses_the_path_that_places_an_order(login: None) -> None:
    client = _client()
    with pytest.raises(CartRefusal, match="checkout, order or payment"):
        client._request("POST", "/rest/V1/carts/mine/order")


@pytest.mark.parametrize(
    "path",
    [
        "/checkout/onepage/success/",
        "/rest/V1/carts/mine/payment-information",
        "/paypal/express/start/",
        "/rest/V1/carts/mine/shipping-information",
    ],
)
def test_refuses_every_checkout_shaped_path(login: None, path: str) -> None:
    with pytest.raises(CartRefusal):
        _client()._request("POST", path)


def test_refuses_a_path_that_is_merely_unlisted(login: None) -> None:
    with pytest.raises(CartRefusal, match="not one of the four paths"):
        _client()._request("GET", "/rest/V1/customers/me")


def test_refuses_the_methods_that_order_and_empty(login: None) -> None:
    for method in ("PUT", "DELETE", "PATCH"):
        with pytest.raises(CartRefusal, match="never"):
            _client()._request(method, CART_PATH)


def test_a_missing_login_stops_the_run_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NAR_EMAIL", raising=False)
    monkeypatch.delenv("NAR_PASSWORD", raising=False)
    with pytest.raises(CartUnavailable, match="Nothing has been staged"):
        _client().read_cart()


def test_a_refused_login_does_not_echo_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAR_EMAIL", "someone@example.invalid")
    monkeypatch.setenv("NAR_PASSWORD", "local-test-only")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"message": "wrong someone@example.invalid"})
    )
    with pytest.raises(CartUnavailable) as raised:
        NarCartClient(transport=transport).read_cart()
    assert "someone@example.invalid" not in str(raised.value)
    assert "401" in str(raised.value)


def test_a_dead_site_is_unknown_not_empty(monkeypatch: pytest.MonkeyPatch, login: None) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=TOKEN)
        if request.url.path.endswith("token")
        else httpx.Response(503, text="down")
    )
    with pytest.raises(CartUnavailable, match="503"):
        NarCartClient(transport=transport).read_cart()


def test_zero_quantity_is_not_a_line(login: None) -> None:
    with pytest.raises(CartRefusal, match="not a quantity"):
        _client().add_line("80-0167", 0)


def test_the_saved_cart_reads_but_cannot_be_added_to(tmp_path: Path) -> None:
    (tmp_path / "nar_cart.json").write_text(
        json.dumps({"id": "q1", "grand_total": "5.00", "items": [{"sku": "A", "qty": 2}]})
    )
    cart = NarFixtureCart(fixture_dir=tmp_path)
    assert cart.read_cart().quantity_of("A") == 2
    with pytest.raises(CartRefusal, match="not the cart"):
        cart.add_line("A", 1)


def test_a_missing_saved_cart_is_not_an_empty_cart(tmp_path: Path) -> None:
    with pytest.raises(CartUnavailable, match="not an empty one"):
        NarFixtureCart(fixture_dir=tmp_path).read_cart()
