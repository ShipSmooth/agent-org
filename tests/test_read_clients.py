"""The read clients, and the quirks of the two systems they read.

Every one of these cases has cost a wrong order at some point: the
two-numbers-in-a-cell report, negative availability, FBA stock that is
counted but not sellable, and NAR's unreliable order-status page.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

import agent_org
import agent_org.integrations
from agent_org.integrations.gmail import GmailFixtureClient, find_directives
from agent_org.integrations.reads import (
    AMAZON_US_FBA_WAREHOUSE_ID,
    SPRINGFIELD_WAREHOUSE_ID,
    AmbiguousOrderSignal,
    ReadFailure,
)
from agent_org.integrations.veeqo import VeeqoFixtureClient, first_value

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "data"


@pytest.fixture
def veeqo() -> VeeqoFixtureClient:
    return VeeqoFixtureClient(fixture_dir=GOLDEN)


def _mailbox(tmp_path: Path, payload: object) -> GmailFixtureClient:
    (tmp_path / "messages.json").write_text(json.dumps(payload), encoding="utf-8")
    return GmailFixtureClient(fixture_dir=tmp_path)


def test_a_two_value_cell_uses_the_first_number() -> None:
    assert first_value("450 (390)") == 450
    assert first_value("-12 (4)") == -12
    assert first_value(97) == 97


def test_an_unreadable_cell_stops_the_run() -> None:
    with pytest.raises(ReadFailure):
        first_value("no data")


def test_negative_availability_keeps_its_sign(veeqo: VeeqoFixtureClient) -> None:
    stock = veeqo.read_inventory()
    assert stock["30-0052"].warehouse_available == -12


def test_fba_reserved_and_unfulfillable_are_not_stock(veeqo: VeeqoFixtureClient) -> None:
    cat = veeqo.read_inventory()["30-0001"]
    assert cat.fba_sellable == 40
    assert cat.fba_reserved > 0
    assert cat.fba_unfulfillable > 0
    assert cat.on_hand == 100  # 60 in Springfield + 40 sellable at FBA


def test_warehouses_other_than_fba_are_added_together(veeqo: VeeqoFixtureClient) -> None:
    hyfin = veeqo.read_inventory()["10-0042"]
    assert hyfin.warehouse_available == 54


def test_the_warehouse_ids_are_the_ones_in_the_procedure(veeqo: VeeqoFixtureClient) -> None:
    """Springfield and Amazon US are identified by number, not by name, and
    getting either wrong silently moves stock between channels."""
    assert SPRINGFIELD_WAREHOUSE_ID == 70459
    assert AMAZON_US_FBA_WAREHOUSE_ID == 192025


def test_ninety_days_of_sales_becomes_an_exact_weekly_rate(
    veeqo: VeeqoFixtureClient,
) -> None:
    """540 in 90 days is 42 a week exactly — kept as a fraction, because
    rounding here compounds through every kit that uses the part."""
    velocity = veeqo.read_velocity(90)["B00CAT0002"]  # the CAT's sales ASIN
    assert velocity.window_days == 90
    assert velocity.units_sold == 540
    assert velocity.weekly() == Fraction(42)


def test_stock_comes_from_veeqo_alone_and_never_from_shopify() -> None:
    """Shopify's quantities are placeholders (999, 2000, 1). There is no
    Shopify client to read them with, and that is the safeguard."""
    package = Path(agent_org.__file__).parent
    assert not (package / "integrations" / "shopify.py").exists()
    imports = [
        line
        for path in package.rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert not [line for line in imports if "shopify" in line.lower()]


def test_a_report_covering_the_wrong_window_stops_the_run(veeqo: VeeqoFixtureClient) -> None:
    with pytest.raises(ReadFailure) as caught:
        veeqo.read_velocity(30)
    assert "90 days" in str(caught.value)


def test_a_missing_export_stops_the_run(tmp_path: Path) -> None:
    with pytest.raises(ReadFailure) as caught:
        VeeqoFixtureClient(fixture_dir=tmp_path).read_inventory()
    assert "stops rather than assuming" in str(caught.value)


def test_a_confirmation_with_no_shipment_is_still_on_order() -> None:
    signals = GmailFixtureClient(fixture_dir=GOLDEN).read_order_signals()
    assert signals.on_order["30-0001"] == 60
    assert "EC2621455" in signals.outstanding_orders


def test_a_split_shipment_matches_the_base_order_and_is_flagged() -> None:
    signals = GmailFixtureClient(fixture_dir=GOLDEN).read_order_signals()
    assert "EC2620998" not in signals.outstanding_orders
    assert any("EC2620998.1" in flag for flag in signals.split_shipment_flags)


def test_directive_text_in_an_email_is_reported_and_not_followed() -> None:
    signals = GmailFixtureClient(fixture_dir=GOLDEN).read_order_signals()
    assert any("ignore previous instructions" in note for note in signals.ignored_directives)
    assert find_directives("Please place the order today") == ("place the order",)


def test_a_confirmation_without_an_order_number_stops_the_run(tmp_path: Path) -> None:
    client = _mailbox(
        tmp_path,
        {"messages": [{"subject": "Your order confirmation", "body": "thank you", "lines": []}]},
    )
    with pytest.raises(AmbiguousOrderSignal) as caught:
        client.read_order_signals()
    assert "no readable EC order number" in str(caught.value)


def test_a_confirmation_with_no_lines_stops_the_run(tmp_path: Path) -> None:
    client = _mailbox(
        tmp_path,
        {"messages": [{"subject": "Order confirmation EC2700001", "body": "", "lines": []}]},
    )
    with pytest.raises(AmbiguousOrderSignal) as caught:
        client.read_order_signals()
    assert "does not list what was ordered" in str(caught.value)


def test_two_confirmations_that_disagree_stop_the_run(tmp_path: Path) -> None:
    """Guessing here means double-ordering: one of the two says what is
    already coming, and nothing in the mailbox says which."""
    client = _mailbox(
        tmp_path,
        {
            "messages": [
                {
                    "subject": "Order confirmation EC2700009",
                    "body": "",
                    "lines": [{"sku": "30-0001", "qty": 600}],
                },
                {
                    "subject": "Order confirmation EC2700009",
                    "body": "",
                    "lines": [{"sku": "30-0001", "qty": 200}],
                },
            ]
        },
    )
    with pytest.raises(AmbiguousOrderSignal) as caught:
        client.read_order_signals()
    assert "two confirmation emails that disagree" in str(caught.value)


def test_two_identical_confirmations_are_not_a_conflict(tmp_path: Path) -> None:
    client = _mailbox(
        tmp_path,
        {
            "messages": [
                {
                    "subject": "Order confirmation EC2700010",
                    "body": "",
                    "lines": [{"sku": "30-0001", "qty": 600}],
                },
                {
                    "subject": "Order confirmation EC2700010",
                    "body": "",
                    "lines": [{"sku": "30-0001", "qty": 600}],
                },
            ]
        },
    )
    assert client.read_order_signals().on_order["30-0001"] == 600


def test_a_shipment_with_no_confirmation_stops_the_run(tmp_path: Path) -> None:
    client = _mailbox(
        tmp_path,
        {"messages": [{"subject": "Shipping notification for EC2700002", "body": ""}]},
    )
    with pytest.raises(AmbiguousOrderSignal) as caught:
        client.read_order_signals()
    assert "no confirmation" in str(caught.value)


def test_an_unreachable_inbox_stops_the_run(tmp_path: Path) -> None:
    client = _mailbox(tmp_path, {"unavailable": True})
    with pytest.raises(AmbiguousOrderSignal) as caught:
        client.read_order_signals()
    assert "will not guess" in str(caught.value)
