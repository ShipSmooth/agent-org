"""The golden test: the worked example in docs/replenishment.md §10 and
docs/plain-english-overview.md, reproduced end to end from fixtures.

Every number asserted here is quoted from those documents. If this test
fails, either the code or the documents are wrong — and the documents are
not to be edited to make it pass.
"""

from __future__ import annotations

from fractions import Fraction

from agent_org.integrations.gmail import OnOrderResult
from agent_org.integrations.veeqo import VeeqoSnapshot
from agent_org.shannon.calculator import (
    RunResult,
    allocate,
    plan_boxes,
    run_calculation,
)
from agent_org.shannon.config_model import ComponentKey, EntityConfig

FULL_IFAK = ("IFAK-CAT-BLACK", "IFAK-CAT-GREEN", "IFAK-CAT-COYOTE", "IFAK-CAT-MULTICAM")


def _result(cfg: EntityConfig, snap: VeeqoSnapshot, oo: OnOrderResult) -> RunResult:
    return run_calculation(cfg, snap, oo)


def _line(result: RunResult, supplier: str, part: str):
    key = ComponentKey(supplier, part)
    matches = [line for line in result.order_lines if line.key == key]
    assert matches, f"no order line for {key}"
    return matches[0]


def test_golden_config_is_valid(golden_cfg: EntityConfig) -> None:
    from agent_org.shannon.validate import validate

    errors = [i for i in validate(golden_cfg) if i.level == "error"]
    assert errors == [], "\n".join(i.render() for i in errors)


def test_kit_velocity_and_forecast(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot
) -> None:
    """Step 1 — full IFAK 35/wk → 245 kits; Compact 7/wk → 49 kits."""
    from agent_org.shannon.calculator import kit_weekly_velocity

    full = sum(
        sum(kit_weekly_velocity(golden_cfg, golden_cfg.kits[kg], golden_snapshot).values())
        for kg in FULL_IFAK
    )
    compact = sum(
        kit_weekly_velocity(
            golden_cfg, golden_cfg.kits["IFAK-CAT-COMPACT"], golden_snapshot
        ).values()
    )
    assert full == Fraction(35)
    assert compact == Fraction(7)
    assert full * 7 == 245
    assert compact * 7 == 49


def test_step2_cat_tourniquet_30_0001(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """588 gross, 428 net, 600 ordered."""
    line = _line(_result(golden_cfg, golden_snapshot, golden_on_order), "nar", "30-0001")
    assert line.standalone_demand == 294
    assert line.kit_demand == 294
    assert line.gross_demand == 588
    assert line.on_hand == 100  # all warehouses + FBA sellable, never reserved
    assert line.on_order == 60  # Gmail: confirmation with no shipping notification
    assert line.net_requirement == 428
    assert line.moq_rounded == 600
    assert line.order_units == 600
    assert line.purchase_units == 600
    assert line.actual_units == 600


def test_step3_hyfin_10_0042(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """441 gross, 387 net, 750 ordered (MOQ minimum)."""
    line = _line(_result(golden_cfg, golden_snapshot, golden_on_order), "nar", "10-0042")
    assert line.standalone_demand == 147
    assert line.kit_demand == 294
    assert line.gross_demand == 441
    assert line.on_hand == 54
    assert line.net_requirement == 387
    assert line.moq_rounded == 750
    assert line.order_units == 750
    assert line.purchase_units == 750
    assert line.actual_units == 750


def test_step4_airway_zz_0034_pack_conversion(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """245 needed, 130 sellable units, 65 two-packs — 65 is what goes in a cart."""
    line = _line(_result(golden_cfg, golden_snapshot, golden_on_order), "nar", "ZZ-0034")
    assert line.standalone_demand == 0  # purchase side only, no sales_asin
    assert line.kit_demand == 245  # full IFAK only
    assert line.on_hand == 118
    assert line.net_requirement == 127
    assert line.order_units == 130
    assert line.units_per_purchase_unit == 2
    assert line.purchase_units == 65
    assert line.actual_units == 130


def test_step5_dynarex_3161_top_up(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """80 available, reorder point 100, target 400 → 320 top-up."""
    result = _result(golden_cfg, golden_snapshot, golden_on_order)
    top = next(t for t in result.top_ups if t.key == ComponentKey("dynarex", "3161"))
    assert top.available == 80
    assert top.reorder_point == 100
    assert top.reorder_target == 400
    assert top.top_up == 320


def test_step6_wall_mount_is_zero(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """A non_stocked component is never purchased, and says so."""
    result = _result(golden_cfg, golden_snapshot, golden_on_order)
    key = ComponentKey("internal", "WALL-MOUNT")
    assert all(line.key != key for line in result.order_lines)
    assert all(t.key != key for t in result.top_ups)
    ns = next(n for n in result.non_stocked if n.key == key)
    assert ns.purchase_units == 0


def test_step7_builds_125_and_19(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """Full IFAK 245 less 120 assembled = 125; Compact 49 less 30 = 19."""
    result = _result(golden_cfg, golden_snapshot, golden_on_order)
    by_kit = {b.kit_group: b for b in result.builds}
    assert sum(by_kit[kg].build for kg in FULL_IFAK) == 125
    assert sum(by_kit[kg].assembled for kg in FULL_IFAK) == 120
    assert sum(by_kit[kg].demand for kg in FULL_IFAK) == 245
    assert by_kit["IFAK-CAT-COMPACT"].build == 19
    assert by_kit["IFAK-CAT-COMPACT"].assembled == 30


def test_step7_limiting_components_named(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """Coyote and Multicam pouch stock is zero — those colourways are blocked."""
    result = _result(golden_cfg, golden_snapshot, golden_on_order)
    by_kit = {b.kit_group: b for b in result.builds}
    for kg, part in (
        ("IFAK-CAT-COYOTE", "IFAK-CAT-COYOTE-bag"),
        ("IFAK-CAT-MULTICAM", "IFAK-CAT-MULTICAM-bag"),
    ):
        assert by_kit[kg].feasible_units == 0
        assert by_kit[kg].limiting_component is not None
        assert part in by_kit[kg].limiting_component


def test_step8_channel_allocation_full_ifak() -> None:
    """§10 step 8, on the document's aggregated full-IFAK figures."""
    alloc = allocate(
        warehouse_on_hand=40,
        fba_on_hand=80,
        fba_inbound=0,
        mf_weekly_velocity=Fraction(6),  # FBM 4 + Shopify 2
        fba_weekly_velocity=Fraction(29),
        mf_floor_weeks=Fraction(2),
        fba_cover_weeks=Fraction(8),
        walmart_reserve_units=0,
        sku="IFAK-CAT (all colourways)",
    )
    assert alloc.mf_floor == 12
    assert alloc.allocatable == 28
    assert alloc.fba_target == 232
    assert alloc.fba_send == 28


def test_step9_box_plan_5_by_48() -> None:
    """§8 and §10 step 9: a 240-unit shipment is 5 boxes of 48, exactly."""
    plan = plan_boxes({"IFAK-CAT-BLACK-FBA": 240}, box_min=5, box_max=10, overship_tolerance=0)
    assert plan is not None
    assert plan.boxes == 5
    assert plan.lines[0].per_box == 48
    assert plan.total_error == 0


def test_box_plan_documented_edge_cases() -> None:
    """Section 8's other worked examples: 250 is 5 boxes of 50 exactly;
    253 is 6 boxes of 42, one unit short."""
    exact = plan_boxes({"X": 250}, box_min=5, box_max=10, overship_tolerance=0)
    assert exact is not None
    assert (exact.boxes, exact.lines[0].per_box, exact.total_error) == (5, 50, 0)

    approx = plan_boxes({"X": 253}, box_min=5, box_max=10, overship_tolerance=0)
    assert approx is not None
    assert (approx.boxes, approx.lines[0].per_box) == (6, 42)
    assert approx.lines[0].planned == 252
    assert approx.total_error == 1


def test_supplier_routing_split(
    golden_cfg: EntityConfig, golden_snapshot: VeeqoSnapshot, golden_on_order: OnOrderResult
) -> None:
    """§10 step 10 — internal and unsourced are prompts, never purchases."""
    result = _result(golden_cfg, golden_snapshot, golden_on_order)
    routes = {t.key: t.routing for t in result.top_ups}
    assert routes[ComponentKey("internal", "HMZ-0001")] == "prompt"
    assert routes[ComponentKey("unsourced", "GLOVE-BLK-L")] == "prompt"
    assert routes[ComponentKey("dynarex", "3161")] == "dynarex_cart"
    assert routes[ComponentKey("world_richman", "IFAK-CAT-COYOTE-bag")] == "gap_list"
