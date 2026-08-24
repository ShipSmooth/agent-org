"""Amazon identity, and what an inactive listing really means.

Two ideas are tested here, and they are the same idea twice.

The first: Amazon's SKU for a product is Amazon's, unguessable from
Zach's, and the only thing Veeqo can be joined on. The ASIN cannot do that
job — North American Rescue owns the C-A-T listings, three colourways
share them, and no title states a colour — so the ASIN is description and
the channel SKU is the key.

The second: a listing Zach took down because he was out of stock sells
nothing, and a trailing average cannot tell that apart from a product
nobody wants. Left alone that buries every product he has ever run out of.
Shannon reports such a line as suppressed, says what it used to sell if
the history reaches back, and never forecasts it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from agent_org.config.listings import ACTIVE, INACTIVE
from agent_org.config.loader import load_config
from agent_org.config.models import ComponentClass, ComponentKey, LoadedConfig
from agent_org.config.validate import validate
from agent_org.integrations.reads import OrderSignals, SalesVelocity, StockPosition
from agent_org.shannon.calculator import (
    ComponentPlan,
    ReplenishmentCalculator,
    ReplenishmentResult,
    Sufficiency,
)
from agent_org.shannon.report import ReportContext, render

REPO = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO / "config"
INVALID_CONFIG = Path(__file__).parent / "fixtures" / "invalid" / "config"
WINDOW = 90


@pytest.fixture(scope="module")
def config() -> LoadedConfig:
    loaded, _ = load_config(REAL_CONFIG, "ithrive")
    return loaded


def _sales(sku: str, units: int, window: int = WINDOW) -> SalesVelocity:
    return SalesVelocity(
        sku=sku, units_sold=units, window_days=window, by_channel={"amazon_fba": units}
    )


def _run(
    config: LoadedConfig,
    velocity: dict[str, SalesVelocity],
    history: dict[str, SalesVelocity] | None = None,
) -> ReplenishmentResult:
    return ReplenishmentCalculator(
        config=config,
        stock={},
        velocity=velocity,
        inbound={},
        on_order={},
        historical_velocity=history,
    ).calculate()


# --- listings are data, statuses included ---------------------------------


def test_a_listing_status_is_read_as_data_not_as_a_comment(config: LoadedConfig) -> None:
    """A plain SKU is live, a {sku, status} block says which, null is none."""
    live = config.listings.for_kit("25-001")
    assert live is not None
    assert live.sku_for("amazon_fba") == "GK-VQI5-LWIS"
    assert all(listing.status == ACTIVE for listing in live.listings)

    down = config.listings.for_kit("25-010")
    assert down is not None
    assert [listing.status for listing in down.listings] == [INACTIVE, INACTIVE]
    assert down.demand_is_suppressed

    half = config.listings.for_kit("26-001")
    assert half is not None
    assert not half.demand_is_suppressed  # FBM is still live

    compact = config.listings.for_kit("IFAK-CAT-COMPACT")
    assert compact is not None
    assert compact.sku_for("amazon_fba") is None  # null: no listing on that channel


def test_a_kit_that_has_never_been_on_amazon_is_not_suppressed(config: LoadedConfig) -> None:
    """20-314 sells on Shopify and direct only. Zero there is the truth, not a
    symptom, and calling it suppressed would cry wolf every week."""
    for kit_group in ("20-314", "20-315", "25-002"):
        listing_set = config.listings.for_kit(kit_group)
        assert listing_set is not None
        assert listing_set.listings == ()
        assert not listing_set.demand_is_suppressed


def test_the_three_cat_colours_are_told_apart_by_seller_sku(config: LoadedConfig) -> None:
    """Seven SKUs, three ASINs, three colours — and the ASIN never decides."""
    orange = config.listings.for_part("30-0023")
    black = config.listings.for_part("30-0001")
    blue = config.listings.for_part("30-0033")
    assert orange is not None and black is not None and blue is not None
    assert set(orange.channel_skus) == {"0Z-RQ1G-J36O", "RJ-54G8-ZQDY", "VR-BODU-ZKN6"}
    assert set(black.channel_skus) == {"5G-AP1S-TUE4", "Q3-MWFF-Y7P4"}
    assert set(blue.channel_skus) == {"DU-AEBP-B9VE", "KV-UTKS-EZMH"}
    # Several channel SKUs share one ASIN, which is exactly why the ASIN
    # cannot be the join: B07CP6Z1C4 alone cannot say which SKU sold.
    assert set(orange.sales_asins) == {"B07CP6Z1C4"}
    assert len(set(orange.channel_skus)) > len(set(orange.sales_asins))


def test_the_blue_training_tourniquet_is_sold_but_never_built(config: LoadedConfig) -> None:
    """30-0033 is resold standalone only: a sales side and a purchase side,
    and no BOM line anywhere."""
    key = ComponentKey(supplier="nar", part="30-0033")
    component = config.boms.components[key]
    assert component.component_class is ComponentClass.FORECAST
    assert (component.moq_min, component.moq_increment) == (400, 200)
    assert not [
        kit.kit_group
        for kit in config.boms.kits.values()
        for line in kit.lines
        if line.component == key
    ]


# --- the channel SKU is the join key --------------------------------------


def test_velocity_is_summed_over_every_channel_sku_of_a_component(
    config: LoadedConfig,
) -> None:
    """Orange C-A-T sells under three SKUs. Its demand is all three.

    The ASIN row is a deliberate decoy: if anything joins on it, the number
    below is wrong by a mile and the test says so.
    """
    result = _run(
        config,
        {
            "0Z-RQ1G-J36O": _sales("0Z-RQ1G-J36O", 100),
            "RJ-54G8-ZQDY": _sales("RJ-54G8-ZQDY", 50),
            "VR-BODU-ZKN6": _sales("VR-BODU-ZKN6", 30),
            "B07CP6Z1C4": _sales("B07CP6Z1C4", 9999),
            "30-0023": _sales("30-0023", 7777),
        },
    )
    orange = result.component("nar", "30-0023")
    assert orange.standalone_units_sold == 180
    assert orange.standalone_weekly == Fraction(180 * 7, WINDOW)


def test_two_colours_sharing_one_asin_do_not_bleed_into_each_other(
    config: LoadedConfig,
) -> None:
    """Black and blue are separate lines even where the listing data overlaps."""
    result = _run(
        config,
        {
            "5G-AP1S-TUE4": _sales("5G-AP1S-TUE4", 40),
            "Q3-MWFF-Y7P4": _sales("Q3-MWFF-Y7P4", 20),
            "DU-AEBP-B9VE": _sales("DU-AEBP-B9VE", 9),
            "KV-UTKS-EZMH": _sales("KV-UTKS-EZMH", 1),
        },
    )
    assert result.component("nar", "30-0001").standalone_units_sold == 60
    assert result.component("nar", "30-0033").standalone_units_sold == 10


def test_a_mapped_component_with_no_channel_sales_reads_zero_not_the_asin(
    config: LoadedConfig,
) -> None:
    """Once a component is mapped, the mapping is the answer even when the
    answer is nothing. Falling through to the ASIN here is exactly how three
    colourways would be merged back into one line."""
    result = _run(config, {"B01ITAKG6A": _sales("B01ITAKG6A", 500)})
    assert result.component("nar", "30-0001").standalone_units_sold == 0


def test_the_asin_survives_in_the_report_as_description(config: LoadedConfig) -> None:
    result = _run(config, {"5G-AP1S-TUE4": _sales("5G-AP1S-TUE4", 40)})
    assert result.component("nar", "30-0001").sales_asins == ("B01ITAKG6A",)


# --- an inactive listing is not zero demand -------------------------------


def _report(config: LoadedConfig, result: ReplenishmentResult) -> str:
    return render(
        result,
        config,
        ReportContext(
            entity_name=config.entity.legal_name,
            generated_at=datetime(2026, 8, 24, tzinfo=UTC),
            config_changes="",
            validation_warnings=(),
            order_signals=OrderSignals(on_order={}),
            data_sources=(),
        ),
    )


def test_a_kit_with_no_sales_and_no_live_listing_is_reported_as_suppressed(
    config: LoadedConfig,
) -> None:
    """25-010 is inactive on both channels: out of stock, so Zach took it down.

    Its velocity is zero, and that zero is about the listing. The line must
    appear, marked, rather than dropping quietly out of the report — the
    products worth restocking are exactly the ones that would be buried.
    """
    result = _run(config, {})
    suppressed = {item.subject for item in result.suppressed}
    assert "25-010" in suppressed
    body = _report(config, result)
    assert "DEMAND SUPPRESSED" in body
    assert "25-010" in body


def test_a_suppressed_line_is_never_forecast(config: LoadedConfig) -> None:
    """Surfaced, not predicted: nothing is ordered off a suppressed reading."""
    result = _run(config, {})
    line = next(item for item in result.suppressed if item.subject == "25-010")
    assert line.current_weekly == Fraction(0)
    assert result.kit("25-010").build_recommendation == 0


def test_suppressed_lines_are_added_to_the_parking_lot_by_the_run(
    config: LoadedConfig,
) -> None:
    """Only Zach can decide to restock and relist, so the decision is filed
    for him rather than waiting for him to notice an absence."""
    result = _run(config, {})
    parked = {addition.id for addition in result.parking_lot_additions}
    assert "AUTO-25-010" in parked
    body = _report(config, result)
    assert "AUTO-25-010" in body
    assert "restock and relist" in body.lower()


def test_history_from_before_the_listing_came_down_is_reported_and_labelled(
    config: LoadedConfig,
) -> None:
    result = _run(
        config,
        {},
        history={
            "#25-010": SalesVelocity(sku="#25-010", units_sold=90, window_days=365),
        },
    )
    line = next(item for item in result.suppressed if item.subject == "25-010")
    assert line.historical_weekly == Fraction(90 * 7, 365)
    assert line.historical_window_days == 365
    body = _report(config, result)
    assert "historical, before it came down" in body


def test_where_history_does_not_reach_back_shannon_says_so_rather_than_zero(
    config: LoadedConfig,
) -> None:
    result = _run(config, {})
    line = next(item for item in result.suppressed if item.subject == "25-010")
    assert line.historical_weekly is None
    assert "no sales history reaches back" in _report(config, result)


def test_a_line_at_zero_for_several_reasons_prints_all_of_them(
    config: LoadedConfig,
) -> None:
    """Orange C-A-T is at zero three times over, and each is a different thing.

    It is sold standalone and sold none; one kit that uses it (25-010) is
    demand-suppressed, which is a listing problem; the other five simply
    sold nothing, which is not. Shown only the first, a reader concludes
    the wrong thing about the other two.
    """
    result = ReplenishmentCalculator(
        config=config,
        stock={"30-0023": StockPosition(sku="30-0023", warehouse_available=500, fba_sellable=0)},
        velocity={},
        inbound={},
        on_order={},
        historical_velocity=None,
    ).calculate()
    plan = next(item for item in result.components if item.key.part == "30-0023")
    reason = plan.sufficiency_reason
    assert plan.sufficiency is Sufficiency.NO_DEMAND, reason
    assert "sold standalone and sold nothing" in reason
    assert "demand-suppressed (25-010)" in reason
    assert "the kits that use it (20-314, 20-315, 25-001, 25-002, 26-001)" in reason
    assert reason.count("; and ") == 2, reason


# --- a part number that is ours, not the supplier's -----------------------


def test_an_internal_reference_without_a_name_is_a_config_error() -> None:
    """Orca publishes no item numbers, so the name is the only orderable
    thing. Without one there is nothing to write on a purchase order."""
    loaded, findings = load_config(INVALID_CONFIG, "ithrive")
    rendered = [
        finding.render()
        for finding in validate(loaded, findings).findings
        if "TBD-NAMELESS-POUCH" in finding.render()
    ]
    assert rendered, "a nameless internal reference was allowed through"
    assert any(text.startswith("ERROR") and "name" in text for text in rendered), rendered


def test_the_orca_pouches_are_ordered_by_name_not_by_our_reference(
    config: LoadedConfig,
) -> None:
    coyote = config.boms.components[
        ComponentKey(supplier="orca_tactical", part="ORCA-MOLLE-EMT-COYOTE")
    ]
    assert coyote.part_is_internal_reference
    assert coyote.order_by == coyote.name
    assert "ORCA-MOLLE-EMT" not in coyote.order_by


def test_the_report_prints_the_product_name_for_an_internal_reference(
    config: LoadedConfig,
) -> None:
    """Otherwise someone raises a purchase order quoting a SKU Orca has
    never heard of, and it is Shannon's report that told them to."""
    key = ComponentKey(supplier="orca_tactical", part="ORCA-MOLLE-EMT-COYOTE")
    component = config.boms.components[key]
    plan = ComponentPlan(
        key=key,
        name=component.name,
        component_class=ComponentClass.REORDER_POINT,
        supplier=key.supplier,
        standalone_units_sold=0,
        standalone_weekly=Fraction(0),
        standalone_demand=Fraction(0),
        kit_demand=Fraction(0),
        fba_prep_demand=Fraction(0),
        safety_stock=Fraction(0),
        gross_demand=Fraction(300),
        on_hand=0,
        on_order=0,
        in_transit=0,
        raw_net=Fraction(300),
        net_units=300,
        moq_rounded=300,
        order_units=300,
        units_per_purchase_unit=1,
        purchase_units=300,
        actual_units=300,
        purchase_unit_name=None,
        routing="purchase_order",
        part_is_internal_reference=True,
    )
    result = _run(config, {})
    result.components = (plan,)
    body = _report(config, result)
    assert f"order by product name: “{component.name}”" in body
    assert "never quote it on a purchase order" in body
    # The name leads and the reference is labelled as ours, so no line in the
    # report can be mistaken for something Orca would recognise.
    assert f"orca_tactical  {component.name}  (our reference {key.part})" in body


# --- one fact, one file ----------------------------------------------------


def test_no_kit_still_carries_a_placeholder_amazon_sku(config: LoadedConfig) -> None:
    """boms.yaml used to carry `amazon_fba: TODO` on every kit. listings.yaml answers
    that question now, so the placeholders were removed rather than copied:
    two files holding the same fact is how they come to disagree."""
    for kit_group, kit in config.boms.kits.items():
        listing_set = config.listings.for_kit(kit_group)
        assert listing_set is not None, f"{kit_group} is not in listings.yaml"
        for channel in set(listing_set.covered_channels) & set(kit.aliases):
            # Where an alias survives it is Veeqo's SKU for that channel, which
            # is Zach's own and is a different thing from Amazon's.
            assert kit.aliases[channel] is not None


def test_listings_yaml_answers_for_amazon_and_for_nothing_else(config: LoadedConfig) -> None:
    """It covers `amazon_fba` and `amazon_fbm` for every kit, including the ones it says have
    no listing. It says nothing about Shopify, so a missing Shopify SKU is
    still a gap and is still reported as one."""
    warnings = [item.message for item in validate(config).warnings]
    assert not [text for text in warnings if "no SKU for the 'amazon_fba' channel" in text]
    assert [text for text in warnings if "IFAK-CAT-COMPACT" in text and "shopify" in text]

    never_listed = config.listings.for_kit("20-314")
    assert never_listed is not None
    assert never_listed.covers("amazon_fba") and never_listed.covers("amazon_fbm")
    assert not never_listed.covers("shopify")


def test_the_training_tourniquet_carries_the_cat_moq_until_nar_says_otherwise(
    config: LoadedConfig,
) -> None:
    """400/200 is NAR's rule for the live colourways and is what the BOM says.
    Nothing on record states the terms for a TRAINING unit, so it is applied as
    written and flagged in the parking lot rather than softened by guesswork."""
    blue = config.boms.components[ComponentKey(supplier="nar", part="30-0033")]
    assert (blue.moq_min, blue.moq_increment) == (400, 200)
    parked = [item for item in config.boms.parking_lot if item.id == "PL-9"]
    assert parked and "400" in (parked[0].detail or "")
