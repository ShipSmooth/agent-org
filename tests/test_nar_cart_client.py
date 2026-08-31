"""The NAR cart client, and the things it must refuse.

The refusals matter more than the happy path: they are the mechanism
behind "Shannon never checks out", and a mechanism nobody tests is a
comment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_org.integrations.carts import CartRefusal, CartUnavailable
from agent_org.integrations.nar import (
    CART_PATH,
    GRAPHQL_PATH,
    ITEMS_PATH,
    TOTALS_PATH,
    NarCartClient,
    NarCatalogue,
    NarFixtureCart,
)

TOKEN = "not-a-real-token"

# The IPOK exactly as narescue.com's own GraphQL described it when Zach ran
# the spike against his account: one configurable parent, three hemostatic
# options, three real child SKUs at three very different prices.
IPOK: dict[str, Any] = {
    "sku": "80-0168-s",
    "name": "Individual Patrol Officer Kit (IPOK)",
    "__typename": "ConfigurableProduct",
    "configurable_options": [
        {"attribute_id": "196", "attribute_code": "hemostatic_agent", "label": "Hemostatic Agent"}
    ],
    "variants": [
        {
            "product": {"sku": "80-0167", "name": "IPOK — Gauze (no Hemostatic)"},
            "attributes": [{"code": "hemostatic_agent", "value_index": 149}],
        },
        {
            "product": {"sku": "80-0168", "name": "IPOK — Combat Gauze"},
            "attributes": [{"code": "hemostatic_agent", "value_index": 146}],
        },
        {
            "product": {"sku": "80-1787", "name": "IPOK — Celox Rapid"},
            "attributes": [{"code": "hemostatic_agent", "value_index": 897}],
        },
    ],
}
TOURNIQUET: dict[str, Any] = {
    "sku": "30-0002",
    "name": "C-A-T Tourniquet GEN 7",
    "__typename": "SimpleProduct",
}


def _catalogue_response(request: httpx.Request, products: list[dict[str, Any]]) -> httpx.Response:
    asked = json.loads(request.content)["variables"]["sku"]
    found = [
        product
        for product in products
        if asked == product["sku"]
        or any(asked == variant["product"]["sku"] for variant in product.get("variants", []))
    ]
    return httpx.Response(200, json={"data": {"products": {"items": found}}})


def _transport(
    seen: list[httpx.Request] | None = None,
    products: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    catalogue = products if products is not None else [IPOK, TOURNIQUET]

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == GRAPHQL_PATH:
            return _catalogue_response(request, catalogue)
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
            # Magento answers with the child SKU the chosen option resolves
            # to, which is why the reply is worth checking at all.
            option = _chosen_option(body)
            sku = _child_for(option) if option is not None else body["sku"]
            return httpx.Response(
                200,
                json={
                    "item_id": 12,
                    "sku": sku,
                    "name": "IPOK",
                    "qty": body["qty"],
                    "price": 38.97,
                },
            )
        raise AssertionError(f"unexpected path {path}")

    return httpx.MockTransport(handle)


def _chosen_option(cart_item: dict[str, Any]) -> int | None:
    options = (
        (cart_item.get("product_option") or {})
        .get("extension_attributes", {})
        .get("configurable_item_options", [])
    )
    return int(options[0]["option_value"]) if options else None


def _child_for(value_index: int) -> str:
    for variant in IPOK["variants"]:
        if variant["attributes"][0]["value_index"] == value_index:
            return str(variant["product"]["sku"])
    raise AssertionError(f"no variant for {value_index}")


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


def test_a_configurable_goes_in_as_its_parent_and_the_option_that_selects_it(
    login: None,
) -> None:
    """The case Zach flagged: 80-0167 is a variant, not a product to post."""
    seen: list[httpx.Request] = []
    NarCartClient(transport=_transport(seen)).add_line("80-0167", 2)
    posted = [request for request in seen if request.url.path == ITEMS_PATH]
    item = json.loads(posted[0].content)["cartItem"]
    assert item["sku"] == "80-0168-s"
    assert item["product_option"]["extension_attributes"]["configurable_item_options"] == [
        {"option_id": "196", "option_value": 149}
    ]


def test_a_simple_product_goes_in_as_itself(login: None) -> None:
    seen: list[httpx.Request] = []
    line = NarCartClient(transport=_transport(seen)).add_line("30-0002", 5)
    posted = next(request for request in seen if request.url.path == ITEMS_PATH)
    item = json.loads(posted.content)["cartItem"]
    assert item["sku"] == "30-0002"
    assert "product_option" not in item
    assert line.quantity == 5


def test_a_parent_sku_is_refused_rather_than_guessed_at(login: None) -> None:
    """'Add the IPOK' does not say which hemostatic agent, so nothing is added."""
    with pytest.raises(CartRefusal, match="does not say what to add"):
        _client().add_line("80-0168-s", 1)


def test_a_sku_the_catalogue_does_not_know_is_refused(login: None) -> None:
    with pytest.raises(CartRefusal, match="no product with SKU 80-9999"):
        _client().add_line("80-9999", 1)


def test_an_unreadable_catalogue_stages_nothing(login: None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == GRAPHQL_PATH:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=TOKEN)

    with pytest.raises(CartUnavailable, match="Nothing was added"):
        NarCartClient(transport=httpx.MockTransport(handle)).add_line("80-0167", 1)


def test_a_line_that_comes_back_as_a_different_product_is_a_failure(login: None) -> None:
    """A cart holding the $110.99 variant instead of the $57.99 one is not success."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == GRAPHQL_PATH:
            return _catalogue_response(request, [IPOK])
        if request.url.path.endswith("/customer/token"):
            return httpx.Response(200, json=TOKEN)
        if request.url.path == CART_PATH:
            return httpx.Response(200, json={"id": 4711, "items": []})
        return httpx.Response(200, json={"sku": "80-0168", "qty": 1, "name": "IPOK"})

    with pytest.raises(CartUnavailable, match="replied with 80-0168"):
        NarCartClient(transport=httpx.MockTransport(handle)).add_line("80-0167", 1)


def test_a_line_that_lands_at_the_wrong_quantity_is_a_failure(login: None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == GRAPHQL_PATH:
            return _catalogue_response(request, [TOURNIQUET])
        if request.url.path.endswith("/customer/token"):
            return httpx.Response(200, json=TOKEN)
        if request.url.path == CART_PATH:
            return httpx.Response(200, json={"id": 4711, "items": []})
        return httpx.Response(200, json={"sku": "30-0002", "qty": 1, "name": "C-A-T"})

    with pytest.raises(CartUnavailable, match="put 1 in"):
        NarCartClient(transport=httpx.MockTransport(handle)).add_line("30-0002", 40)


def test_a_variant_the_catalogue_cannot_explain_is_refused() -> None:
    """The parent lists the child but never says which option picks it."""
    unexplained = dict(IPOK, configurable_options=[])
    catalogue = NarCatalogue(transport=_transport(products=[unexplained]))
    with pytest.raises(CartRefusal, match="does not say which 'hemostatic_agent' selects it"):
        catalogue.resolve("80-0167")


def test_a_child_the_parent_does_not_list_is_refused() -> None:
    """GraphQL answered, but with a product this SKU is not a variant of."""
    variants = [v for v in IPOK["variants"] if v["product"]["sku"] != "80-0167"]
    answer = {"data": {"products": {"items": [dict(IPOK, variants=variants)]}}}
    catalogue = NarCatalogue(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=answer))
    )
    with pytest.raises(CartRefusal, match="no product with SKU 80-0167"):
        catalogue.resolve("80-0167")


def test_the_catalogue_resolves_the_variant_zach_actually_buys() -> None:
    item = NarCatalogue(transport=_transport()).resolve("80-0167")
    assert (item.sku, item.expect_sku, item.options) == (
        "80-0168-s",
        "80-0167",
        (("196", 149),),
    )


# What narescue.com's own search does with a component part number: the
# kits that contain it come back too, and they come back first.
KIT_WITH_THE_PART: dict[str, Any] = {
    "sku": "85-0715",
    "name": "Public Access Bleeding Control Station",
    "__typename": "SimpleProduct",
}
THE_PART: dict[str, Any] = {
    "sku": "30-0052",
    "name": "Combat Gauze LE",
    "__typename": "SimpleProduct",
}


def _fuzzy_transport(products: list[dict[str, Any]]) -> httpx.MockTransport:
    """A catalogue that answers a SKU filter with everything that mentions it."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == GRAPHQL_PATH
        return httpx.Response(200, json={"data": {"products": {"items": products}}})

    return httpx.MockTransport(handle)


def test_a_kit_that_merely_contains_the_part_is_not_mistaken_for_the_part() -> None:
    """narescue.com answers 30-0052 with the station that holds one, first.

    Taking the first answer made a stocked part look like a wrong part
    number, so every answer is read and only the part itself is staged.
    """
    catalogue = NarCatalogue(transport=_fuzzy_transport([KIT_WITH_THE_PART, THE_PART]))
    item = catalogue.resolve("30-0052")
    assert (item.sku, item.expect_sku, item.name) == ("30-0052", "30-0052", "Combat Gauze LE")


def test_a_variant_is_still_found_when_it_is_not_the_first_answer() -> None:
    catalogue = NarCatalogue(transport=_fuzzy_transport([KIT_WITH_THE_PART, IPOK]))
    item = catalogue.resolve("80-0167")
    assert (item.sku, item.expect_sku, item.options) == ("80-0168-s", "80-0167", (("196", 149),))


def test_only_loose_matches_means_the_part_number_is_wrong_and_says_so() -> None:
    """No exact match anywhere is the one case that is a parts-list error."""
    catalogue = NarCatalogue(transport=_fuzzy_transport([KIT_WITH_THE_PART]))
    with pytest.raises(CartRefusal, match="no product with SKU 30-0052") as refusal:
        catalogue.resolve("30-0052")
    assert "85-0715" in str(refusal.value)


# 82-0075 is a part Zach buys weekly. The SKU filter answers with nothing
# at all for it, because the catalogue holds it only as a child of the
# configurable 82-0075-c — a search finds that parent, the filter does not.
TPAK_PARENT: dict[str, Any] = {
    "sku": "82-0075-c",
    "name": "Mini Trail Personal Aid Kit (Mini TPAK) - LOKSAK",
    "__typename": "ConfigurableProduct",
    "configurable_options": [
        {"attribute_id": "201", "attribute_code": "kit_contents", "label": "Contents"}
    ],
    "variants": [
        {
            "product": {"sku": "82-0075", "name": "Mini Trail Personal Aid Kit - Basic"},
            "attributes": [{"code": "kit_contents", "value_index": 310}],
        },
        {
            "product": {"sku": "82-0076", "name": "Mini Trail Personal Aid Kit - with BCD"},
            "attributes": [{"code": "kit_contents", "value_index": 311}],
        },
    ],
}


def _filter_then_search(on_search: list[dict[str, Any]]) -> httpx.MockTransport:
    """A catalogue whose SKU filter finds nothing and whose search does."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == GRAPHQL_PATH
        body = json.loads(request.content)
        found = [] if "filter:" in body["query"] else on_search
        return httpx.Response(200, json={"data": {"products": {"items": found}}})

    return httpx.MockTransport(handle)


def test_a_part_the_sku_filter_cannot_see_is_found_by_searching() -> None:
    catalogue = NarCatalogue(transport=_filter_then_search([TPAK_PARENT]))
    item = catalogue.resolve("82-0075")
    assert (item.sku, item.expect_sku, item.options) == ("82-0075-c", "82-0075", (("201", 310),))


def test_the_search_fallback_is_a_wider_net_and_not_a_looser_standard() -> None:
    """The search answers, but with something that is not the part."""
    catalogue = NarCatalogue(transport=_filter_then_search([KIT_WITH_THE_PART]))
    with pytest.raises(CartRefusal, match="no product with SKU 82-0075") as refusal:
        catalogue.resolve("82-0075")
    assert "85-0715" in str(refusal.value)


def test_a_sku_neither_way_knows_is_reported_as_a_parts_list_error() -> None:
    catalogue = NarCatalogue(transport=_filter_then_search([]))
    with pytest.raises(CartRefusal, match="by filter or by search"):
        catalogue.resolve("99-9999")


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
    with pytest.raises(CartRefusal, match="not one of the paths"):
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
