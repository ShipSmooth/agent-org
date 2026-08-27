"""What the plan takes from the report, and what it refuses to invent."""

from __future__ import annotations

from typing import Any

from agent_org.shannon.staging import plan_from_report_lines


def line(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "component": "nar/30-0002",
        "name": "C-A-T Tourniquet",
        "rounded_to_five": 400,
        "purchase_units": None,
        "units_per_purchase_unit": None,
        "purchase_unit_name": None,
        "part_is_internal_reference": False,
        "routing": "nar_cart",
    }
    base.update(over)
    return base


def test_only_lines_routed_to_this_cart_are_staged() -> None:
    plan = plan_from_report_lines(
        [line(), line(component="dynarex/1234", routing="dynarex_cart")], "nar"
    )
    assert [staged.sku for staged in plan.lines] == ["30-0002"]


def test_the_quantity_is_purchase_units_not_units() -> None:
    """A hundred units of something sold in 25s is four boxes, not a hundred."""
    plan = plan_from_report_lines(
        [
            line(
                rounded_to_five=100,
                purchase_units=4,
                units_per_purchase_unit=25,
                purchase_unit_name="case",
            )
        ],
        "nar",
    )
    staged = plan.lines[0]
    assert staged.quantity == 4
    assert staged.how_much == "4 × case (25 each = 100 units)"


def test_our_own_reference_is_not_offered_to_the_supplier_as_a_sku() -> None:
    plan = plan_from_report_lines(
        [line(component="nar/IFAK-BAG-COYOTE", part_is_internal_reference=True)], "nar"
    )
    assert plan.lines == ()
    assert "our own reference" in plan.skipped[0].reason


def test_a_cart_line_with_nothing_to_order_is_reported_not_dropped() -> None:
    plan = plan_from_report_lines([line(rounded_to_five=0)], "nar")
    assert plan.lines == ()
    assert "orders none of it" in plan.skipped[0].reason


def test_a_gap_list_line_is_not_staged() -> None:
    assert not plan_from_report_lines([line(routing="gap_list")], "nar")
