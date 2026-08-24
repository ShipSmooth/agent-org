"""The shipped sample fixture must be able to fail.

The channel SKU join is the single mechanism keeping the three C-A-T
colourways apart: North American Rescue owns those listings, three
colours share three ASINs and no title states a colour, so Zach's own
seller-SKUs are the only thing that says which tourniquet sold. Merge
them and the first consequence is a 400-unit order of the wrong colour.

The sample sales fixture used to carry a row keyed on Zach's own part
number whose total happened to equal the sum of that part's channel
SKUs, so summing the channel SKUs and reading the part row gave the
same answer and no test could tell a working join from a broken one.
That is now deliberately impossible: every part-keyed row is a decoy
with a figure that matches nothing.

The two halves of this file are the point. The first asserts what the
join produces. The second proves those assertions are load-bearing:
delete the decoys and nothing moves; break one mapping and it does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path

import pytest

from agent_org.config.loader import load_config
from agent_org.config.models import LoadedConfig
from agent_org.integrations.gmail import GmailFixtureClient
from agent_org.integrations.reads import SalesVelocity
from agent_org.integrations.veeqo import VeeqoFixtureClient
from agent_org.shannon.calculator import ReplenishmentCalculator, ReplenishmentResult

REPO = Path(__file__).resolve().parents[1]
SAMPLE = Path(__file__).parent / "fixtures" / "ithrive-sample"
WINDOW = 90

# What the fixture says, and what it must never be confused with. The
# three sums are unequal to each other and to every decoy, so no wrong
# join can arrive at a right number by luck.
BLACK, ORANGE, BLUE = 138 + 47, 53 + 29 + 11, 61 + 24
DECOYS = {"30-0001": 999, "30-0023": 4, "30-0033": 500}


@pytest.fixture(scope="module")
def config() -> LoadedConfig:
    loaded, _ = load_config(REPO / "config", "ithrive")
    return loaded


def _velocity(edit: Mapping[str, int | None] | None = None) -> dict[str, SalesVelocity]:
    """The sample sales export, optionally with rows removed or altered.

    `None` deletes a row; an integer rewrites its total, which is how a
    broken mapping is simulated without touching the code under test.
    """
    inventory = VeeqoFixtureClient(fixture_dir=SAMPLE)
    rows = inventory.read_velocity(WINDOW)
    for sku, units in (edit or {}).items():
        if units is None:
            rows.pop(sku, None)
        else:
            rows[sku] = SalesVelocity(sku=sku, units_sold=units, window_days=WINDOW)
    return rows


def _run(config: LoadedConfig, edit: Mapping[str, int | None] | None = None) -> ReplenishmentResult:
    inventory = VeeqoFixtureClient(fixture_dir=SAMPLE)
    orders = GmailFixtureClient(fixture_dir=SAMPLE)
    return ReplenishmentCalculator(
        config=config,
        stock=inventory.read_inventory(),
        velocity=_velocity(edit),
        inbound=inventory.read_fba_inbound(),
        on_order=orders.read_order_signals().on_order,
        historical_velocity=inventory.read_velocity_history(),
    ).calculate()


def _standalone(result: ReplenishmentResult) -> dict[str, int]:
    return {part: result.component("nar", part).standalone_units_sold for part in DECOYS}


# --- what the fixture asserts ---------------------------------------------


def test_each_colourway_reads_the_sum_of_its_own_channel_skus(config: LoadedConfig) -> None:
    """Three colours, seven Amazon SKUs, three different answers."""
    assert _standalone(_run(config)) == {"30-0001": BLACK, "30-0023": ORANGE, "30-0033": BLUE}


def test_no_colourway_can_be_mistaken_for_another_or_for_a_decoy(config: LoadedConfig) -> None:
    """The fixture's arithmetic property, asserted rather than assumed: if
    any two of these coincided, a merge or a wrong-row read could pass."""
    figures = [BLACK, ORANGE, BLUE, BLACK + ORANGE + BLUE, *DECOYS.values()]
    assert len(set(figures)) == len(figures)


def test_the_weekly_figures_follow_the_units_and_stay_exact(config: LoadedConfig) -> None:
    result = _run(config)
    for part, units in (("30-0001", BLACK), ("30-0023", ORANGE), ("30-0033", BLUE)):
        assert result.component("nar", part).standalone_weekly == Fraction(units * 7, WINDOW)


def test_the_blue_training_tourniquet_orders_at_nars_minimum(config: LoadedConfig) -> None:
    """Blue is resold standalone and in no kit, so its whole demand comes
    through the join — and NAR's 400 minimum turns a small demand into a
    large cheque. Getting its sales from the wrong row is real money."""
    blue = _run(config).component("nar", "30-0033")
    assert blue.kit_demand == Fraction(0)
    assert blue.standalone_units_sold == BLUE
    assert blue.order_units == 400


# --- proof those assertions are load-bearing ------------------------------


def test_deleting_the_part_keyed_rows_changes_nothing_at_all(config: LoadedConfig) -> None:
    """The negative case. Zach's own part numbers are not Amazon's, so a
    row keyed on one must not reach the report — and the way to prove
    that is to delete all three and find the run identical, not merely
    still passing."""
    before, after = _run(config), _run(config, {part: None for part in DECOYS})
    assert _standalone(after) == {"30-0001": BLACK, "30-0023": ORANGE, "30-0033": BLUE}
    assert [(plan.key, plan.order_units) for plan in after.components] == [
        (plan.key, plan.order_units) for plan in before.components
    ]


def test_corrupting_the_part_keyed_rows_changes_nothing_either(config: LoadedConfig) -> None:
    """Deleting a row it never reads is weak evidence on its own; a row
    screaming a wrong number at it is stronger."""
    poison = {part: 100_000 for part in DECOYS}
    assert _standalone(_run(config, poison)) == {
        "30-0001": BLACK,
        "30-0023": ORANGE,
        "30-0033": BLUE,
    }


def test_breaking_one_channel_sku_mapping_does_change_the_answer(config: LoadedConfig) -> None:
    """And the other half: a test that cannot fail is not a test. Drop
    black's FBA SKU and black must fall by exactly that SKU's sales,
    while orange and blue stand still."""
    broken = _run(config, {"Q3-MWFF-Y7P4": None})
    assert broken.component("nar", "30-0001").standalone_units_sold == BLACK - 47
    assert broken.component("nar", "30-0023").standalone_units_sold == ORANGE
    assert broken.component("nar", "30-0033").standalone_units_sold == BLUE


def test_a_merged_join_would_be_visible_rather_than_plausible(config: LoadedConfig) -> None:
    """The failure this exists to catch: all seven SKUs landing on one
    colour. It is caught because 185, 93 and 85 are not interchangeable."""
    merged = BLACK + ORANGE + BLUE
    for part, honest in (("30-0001", BLACK), ("30-0023", ORANGE), ("30-0033", BLUE)):
        assert _run(config).component("nar", part).standalone_units_sold == honest != merged


def test_the_fixture_file_itself_says_the_decoys_are_decoys(config: LoadedConfig) -> None:
    """A later hand tidying the fixture must not be able to 'fix' the
    inequality back into a coincidence without reading why."""
    rows = json.loads((SAMPLE / "velocity.json").read_text(encoding="utf-8"))["rows"]
    totals = {str(row["sku"]): row["units_sold"] for row in rows}
    assert {part: totals[part] for part in DECOYS} == DECOYS
    assert totals["5G-AP1S-TUE4"] + totals["Q3-MWFF-Y7P4"] != totals["30-0001"]
