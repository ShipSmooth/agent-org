"""Veeqo read client — the quirks in Zach's working procedure are facts."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from agent_org.integrations.veeqo import (
    FBA_WAREHOUSE_ID,
    SPRINGFIELD_WAREHOUSE_ID,
    VeeqoReadClient,
    VeeqoReadError,
    VeeqoSnapshot,
    first_value,
)


def test_warehouse_ids_are_the_documented_ones() -> None:
    assert SPRINGFIELD_WAREHOUSE_ID == 70459
    assert FBA_WAREHOUSE_ID == 192025


def test_two_values_per_cell_uses_the_first() -> None:
    """The products report shows current then comparison period."""
    assert first_value("180 / 150") == 180
    assert first_value("1,240 / 900") == 1240
    assert first_value(42) == 42


def test_first_value_keeps_a_negative_sign() -> None:
    assert first_value("-3 / 5") == -3


def test_first_value_refuses_to_guess() -> None:
    with pytest.raises(VeeqoReadError):
        first_value("n/a")


def test_negative_available_is_real_and_keeps_its_sign(golden_snapshot: VeeqoSnapshot) -> None:
    assert golden_snapshot.stock["80-0027"].warehouse_available == -3


def test_fba_sellable_counts_reserved_and_unfulfillable_do_not(
    golden_snapshot: VeeqoSnapshot,
) -> None:
    level = golden_snapshot.stock["30-0001"]
    assert level.warehouse_available == 90
    assert level.fba_sellable == 10  # reserved 5 and unfulfillable 2 excluded
    assert level.on_hand == 100


def test_velocity_window_is_ninety_days(golden_snapshot: VeeqoSnapshot) -> None:
    assert golden_snapshot.window_days == 90
    # 540 units in 90 days = 42 per week, exactly (no float drift).
    assert golden_snapshot.total_weekly_velocity("30-0001") == Fraction(42)


def test_shopify_inventory_is_never_read_as_stock(golden_data_dir: Path, tmp_path: Path) -> None:
    """Shopify quantities are placeholders (docs/agents.md)."""
    for name in ("veeqo_stock.json", "veeqo_products_report.json", "veeqo_fba_inbound.json"):
        (tmp_path / name).write_text((golden_data_dir / name).read_text(), encoding="utf-8")
    stock = json.loads((tmp_path / "veeqo_stock.json").read_text())
    stock["products"].append(
        {"sku": "30-0001", "stock_entries": [{"warehouse_id": 999999, "available": 99999}]}
    )
    (tmp_path / "veeqo_stock.json").write_text(json.dumps(stock), encoding="utf-8")
    snap = VeeqoReadClient(tmp_path).snapshot()
    # An unknown location (Shopify's placeholder store) contributes nothing.
    assert snap.stock["30-0001"].on_hand == 100


def test_missing_fixture_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(VeeqoReadError):
        VeeqoReadClient(tmp_path).snapshot()
