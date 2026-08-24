"""The read clients, and the quirks of the two systems they read.

Every one of these cases has cost a wrong order at some point: the
two-numbers-in-a-cell report, negative availability, FBA stock that is
counted but not sellable, and NAR's unreliable order-status page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_org.integrations.gmail import GmailFixtureClient, find_directives
from agent_org.integrations.reads import AmbiguousOrderSignal, ReadFailure
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
