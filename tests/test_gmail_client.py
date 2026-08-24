"""Gmail on_order — the authoritative outstanding-order signal (§3.1).

NAR's own order-status field is unreliable and is never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_org.integrations.gmail import (
    CONFIRMATION_SUBJECT,
    NAR_SENDER,
    SHIPPING_SUBJECT_PREFIX,
    GmailReadClient,
    GmailReadError,
    OnOrderResult,
)


def _client(tmp_path: Path, messages: list[dict[str, str]]) -> GmailReadClient:
    (tmp_path / "gmail_messages.json").write_text(json.dumps(messages), encoding="utf-8")
    return GmailReadClient(tmp_path)


def _confirmation(order: str, body_lines: str) -> dict[str, str]:
    return {
        "subject": CONFIRMATION_SUBJECT,
        "from": NAR_SENDER,
        "body": f"Thank you for your order {order}.\n\nItems:\n{body_lines}",
    }


def _shipping(order: str) -> dict[str, str]:
    return {
        "subject": f"{SHIPPING_SUBJECT_PREFIX}{order}",
        "from": NAR_SENDER,
        "body": f"Your order {order} has shipped.",
    }


def test_confirmation_without_shipping_is_on_order(golden_on_order: OnOrderResult) -> None:
    assert golden_on_order.units_on_order("30-0001") == 60


def test_confirmation_with_shipping_is_not_on_order(golden_on_order: OnOrderResult) -> None:
    assert golden_on_order.units_on_order("10-0042") == 0


def test_matching_is_on_the_base_ec_number(tmp_path: Path) -> None:
    """EC2620998.1 ships EC2620998 — the suffix is a split, not a new order."""
    client = _client(
        tmp_path, [_confirmation("EC2620998", "10-0042 x 300\n"), _shipping("EC2620998.1")]
    )
    result = client.on_order()
    assert result.units_on_order("10-0042") == 0


def test_split_suffix_is_flagged(golden_on_order: OnOrderResult) -> None:
    assert any("EC2620998" in flag for flag in golden_on_order.split_shipment_flags)


def test_ambiguous_signal_stops_the_run(tmp_path: Path) -> None:
    """Two confirmations for the same order number — Shannon never guesses."""
    client = _client(
        tmp_path,
        [
            _confirmation("EC2620990", "30-0001 x 60\n"),
            _confirmation("EC2620990", "30-0001 x 120\n"),
        ],
    )
    with pytest.raises(GmailReadError):
        client.on_order()


def test_unreadable_mailbox_stops_the_run(tmp_path: Path) -> None:
    with pytest.raises(GmailReadError):
        GmailReadClient(tmp_path).on_order()


def test_confirmation_with_no_order_number_stops_the_run(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        [{"subject": CONFIRMATION_SUBJECT, "from": NAR_SENDER, "body": "Thanks for your order."}],
    )
    with pytest.raises(GmailReadError):
        client.on_order()


def test_directive_text_in_mail_is_data_not_instructions(golden_on_order: OnOrderResult) -> None:
    """The fixture contains a mail telling Shannon to buy everything; it is ignored."""
    assert [o.order_number for o in golden_on_order.outstanding] == ["EC2620990"]
