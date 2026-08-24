"""The golden test: the worked example, number for number.

Every figure asserted here comes from docs/plain-english-overview.md and
docs/replenishment.md §10. If this test and those documents ever disagree,
one of them is wrong and the disagreement is the finding — not something to
paper over by editing whichever side is easier to change.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from agent_org.config.loader import load_config
from agent_org.config.validate import validate
from agent_org.integrations.gmail import GmailFixtureClient
from agent_org.integrations.veeqo import VeeqoFixtureClient
from agent_org.shannon.boxes import plan_boxes
from agent_org.shannon.calculator import ReplenishmentCalculator, ReplenishmentResult

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


@pytest.fixture(scope="module")
def result() -> ReplenishmentResult:
    config, findings = load_config(GOLDEN / "config", "ithrive")
    report = validate(config, findings)
    assert not report.blocking_errors, [finding.message for finding in report.errors]

    veeqo = VeeqoFixtureClient(fixture_dir=GOLDEN / "data")
    gmail = GmailFixtureClient(fixture_dir=GOLDEN / "data")
    signals = gmail.read_order_signals()
    return ReplenishmentCalculator(
        config=config,
        stock=veeqo.read_inventory(),
        velocity=veeqo.read_velocity(config.shannon.parameters.velocity_window_days),
        inbound=veeqo.read_fba_inbound(),
        on_order=signals.on_order,
    ).calculate()


# --- Step 1: kit velocity -------------------------------------------------


def test_full_ifak_family_sells_35_a_week_and_needs_245_kits(
    result: ReplenishmentResult,
) -> None:
    ifak = result.kit("IFAK-CAT")
    assert ifak.weekly_velocity == Fraction(35)
    assert ifak.demand_units == 245


def test_compact_ifak_sells_7_a_week_and_needs_49_kits(result: ReplenishmentResult) -> None:
    compact = result.kit("IFAK-CAT-COMPACT")
    assert compact.weekly_velocity == Fraction(7)
    assert compact.demand_units == 49


# --- Step 2: 30-0001, demand read from the sales side only ----------------


def test_cat_black_orders_600(result: ReplenishmentResult) -> None:
    cat = result.component("nar", "30-0001")
    assert cat.standalone_units_sold == 540
    assert cat.standalone_weekly == Fraction(42)
    assert cat.standalone_demand == 294
    assert cat.kit_demand == 294  # 245 full IFAK + 49 Compact
    assert cat.gross_demand == 588
    assert cat.on_hand == 100  # 60 warehouse + 40 FBA sellable; reserved excluded
    assert cat.on_order == 60  # Gmail: confirmation with no shipping notification
    assert cat.in_transit == 0
    assert cat.raw_net == 428
    assert cat.moq_rounded == 600
    assert cat.order_units == 600
    assert cat.purchase_units == 600
    assert cat.actual_units == 600


# --- Step 3: 10-0042 ------------------------------------------------------


def test_hyfin_orders_750(result: ReplenishmentResult) -> None:
    hyfin = result.component("nar", "10-0042")
    assert hyfin.standalone_units_sold == 270
    assert hyfin.standalone_weekly == Fraction(21)
    assert hyfin.standalone_demand == 147
    assert hyfin.kit_demand == 294
    assert hyfin.gross_demand == 441
    assert hyfin.on_hand == 54
    assert hyfin.on_order == 0
    assert hyfin.raw_net == 387
    assert hyfin.moq_rounded == 750
    assert hyfin.order_units == 750
    assert hyfin.purchase_units == 750
    assert hyfin.actual_units == 750


# --- Step 4: ZZ-0034, the pack-size line ----------------------------------


def test_airway_orders_65_two_packs(result: ReplenishmentResult) -> None:
    airway = result.component("nar", "ZZ-0034")
    assert airway.standalone_demand == 0  # bought, never resold
    assert airway.kit_demand == 245  # full IFAK only
    assert airway.on_hand == 118
    assert airway.raw_net == 127
    assert airway.moq_rounded == 127  # no MOQ
    assert airway.order_units == 130  # nearest five
    assert airway.purchase_units == 65  # sold as a two-pack
    assert airway.actual_units == 130


# --- Step 5: Dynarex 3161, reorder point ----------------------------------


def test_gauze_tops_up_by_320(result: ReplenishmentResult) -> None:
    gauze = result.component("dynarex", "3161")
    assert gauze.on_hand == 80
    assert gauze.on_order == 0
    assert gauze.order_units == 320  # target 400 - available 80
    assert gauze.purchase_units == 320


# --- Step 6: the wall mount, non_stocked ----------------------------------


def test_wall_mount_is_never_bought(result: ReplenishmentResult) -> None:
    mount = result.component("world_richman", "WALL-MOUNT-01")
    assert mount.order_units == 0
    assert mount.purchase_units == 0
    assert mount.actual_units == 0


# --- Step 7: builds -------------------------------------------------------


def test_build_recommendations_are_125_and_19(result: ReplenishmentResult) -> None:
    ifak = result.kit("IFAK-CAT")
    assert ifak.assembled_stock == 120  # warehouse 40 + FBA 80
    assert ifak.build_recommendation == 125

    compact = result.kit("IFAK-CAT-COMPACT")
    assert compact.assembled_stock == 30
    assert compact.build_recommendation == 19


def test_the_family_build_is_split_across_the_colourways_without_drift(
    result: ReplenishmentResult,
) -> None:
    """Both views, one number: the family is forecast together, but the build
    sheet has to say how many of each colourway to make."""
    ifak = result.kit("IFAK-CAT")
    shares = {member.kit_group: member.build_share for member in ifak.members}
    assert sum(shares.values()) == ifak.build_recommendation == 125
    assert all(share >= 0 for share in shares.values())
    # The two colourways with no stock of their own carry the largest shares.
    assert shares["IFAK-CAT-COYOTE"] >= shares["IFAK-CAT-BLACK"]


def test_coyote_and_multicam_cannot_be_built_and_the_pouch_is_named(
    result: ReplenishmentResult,
) -> None:
    blocked = {
        member.kit_group: member
        for member in result.kit("IFAK-CAT").members
        if member.buildable_now == 0
    }
    assert set(blocked) == {"IFAK-CAT-COYOTE", "IFAK-CAT-MULTICAM"}
    for kit_group, member in blocked.items():
        assert member.limiting_component is not None
        assert member.limiting_component.part == f"{kit_group}-bag"


# --- Step 8: channel allocation -------------------------------------------


def test_full_ifak_allocation_sends_28_now(result: ReplenishmentResult) -> None:
    allocation = result.kit("IFAK-CAT").allocation
    assert allocation is not None
    assert allocation.warehouse_on_hand == 40
    assert allocation.mf_floor == 12  # 2 weeks of 4 FBM + 2 Shopify
    assert allocation.allocatable == 28
    assert allocation.fba_target == 232  # 8 weeks of 29 FBA
    assert allocation.fba_on_hand == 80
    assert allocation.fba_inbound == 0
    assert allocation.wanted_at_fba == 152
    assert allocation.fba_send == 28
    assert allocation.walmart_reserve == 0


# --- Step 9: FBA box plan -------------------------------------------------


def test_the_sending_target_is_step_8s_28_not_the_stale_240(
    result: ReplenishmentResult,
) -> None:
    """Settled 21 Aug 2026. §9 used to say the target was 240; that is 8 weeks
    × 30/week, and FBA velocity is 29/week, so the target is 232 against 80
    already at FBA — a want of 152, clamped by §8's 28 allocatable units."""
    assert result.fba_send_targets["IFAK-CAT"] == 28
    plan = result.box_plan
    assert plan is not None
    assert 5 <= plan.boxes <= 10
    # 28 alone would pack exactly as 7 × 4, but the Compact IFAK's 5 units ride
    # in the same shipment and one box count serves both: at 7 boxes the
    # Compact line can send nothing (7 × 1 overships its 5), so 5 boxes — 25
    # full IFAK plus all 5 Compact — is the smaller total error.
    assert plan.boxes == 5
    assert plan.planned("IFAK-CAT") == 25
    assert plan.planned("IFAK-CAT-COMPACT") == 5
    assert plan.error == 3


def test_a_lone_28_unit_line_packs_exactly_as_7_boxes_of_4() -> None:
    """The 28 held no units back for want of a better fit — only for company.

    Asked on its own, the planner finds the exact answer: 7 × 4 = 28, error
    nothing. The tie-break toward fewer boxes applies to ties, and zero error
    is not a tie. A regression here would quietly hold stock back from FBA.
    """
    plan = plan_boxes({"IFAK-CAT": 28}, box_min=5, box_max=10, overship_tolerance=0)
    assert plan is not None
    assert plan.boxes == 7
    assert [line.per_box for line in plan.lines] == [4]
    assert plan.planned("IFAK-CAT") == 28
    assert plan.error == 0


def test_a_240_unit_shipment_packs_as_5_boxes_of_48() -> None:
    """The box planner in its own right: 240 is an exact-fit arithmetic case
    that exercises the tie-break, not a sending target for anything."""
    plan = plan_boxes({"IFAK-CAT": 240}, box_min=5, box_max=10, overship_tolerance=0)
    assert plan is not None
    assert plan.boxes == 5
    assert [line.per_box for line in plan.lines] == [48]
    assert plan.planned("IFAK-CAT") == 240
    assert plan.error == 0


# --- Step 10: supplier split, gap list ------------------------------------


def test_internal_and_unsourced_lines_go_to_the_gap_list_not_a_cart(
    result: ReplenishmentResult,
) -> None:
    gaps = {(entry.key.supplier, entry.key.part) for entry in result.gap_list}
    assert ("internal", "HMZ-0001") in gaps
    assert ("unsourced", "GLOVE-BLK-L") in gaps


def test_negative_availability_keeps_its_sign(result: ReplenishmentResult) -> None:
    """-12 is twelve units short against orders already placed, not zero."""
    gauze = result.component("nar", "30-0052")
    assert gauze.on_hand == -12
    assert gauze.raw_net == 12
    assert gauze.order_units == 15


def test_ops_consumables_are_never_a_purchase_line(result: ReplenishmentResult) -> None:
    assert all(plan.key.part != "B0822QWLX2" for plan in result.components)


def test_kits_are_never_purchased(result: ReplenishmentResult) -> None:
    kit_skus = {"IFAK-CAT-BLACK", "IFAK-CAT-COMPACT", "ESSENTIAL-WALL"}
    assert all(plan.key.part not in kit_skus for plan in result.components)


def test_fba_only_bom_lines_are_charged_against_the_send_quantity(
    result: ReplenishmentResult,
) -> None:
    bag = result.component("amazon_business", "B07X6QZ53J")
    # 28 full-IFAK units + 5 Compact, not the 294 kits of total demand.
    assert bag.fba_prep_demand == 33
    assert bag.kit_demand == 0
