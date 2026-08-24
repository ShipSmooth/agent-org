"""A family's FBA send divides by demand (docs/replenishment.md §7.1).

A kit family ships to FBA as one shipment, so the send quantity is
computed once for the family and then divided across its colourways.
Nothing Zach sent specified how, and the answer was an implementation
detail until he confirmed it: in proportion to each colourway's own
demand, because black outsells green and should get more.

This file holds it to that. The unequal-velocity case is the point — an
even split and a proportional split agree whenever velocities are equal,
so equal velocities cannot tell one rule from the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_org.config.loader import load_config
from agent_org.config.models import LoadedConfig
from agent_org.integrations.gmail import GmailFixtureClient
from agent_org.integrations.reads import SalesVelocity
from agent_org.integrations.veeqo import VeeqoFixtureClient
from agent_org.shannon.calculator import (
    ReplenishmentCalculator,
    ReplenishmentResult,
    _split_by_demand,
)

REPO = Path(__file__).resolve().parents[1]
SAMPLE = Path(__file__).parent / "fixtures" / "ithrive-sample"
WINDOW = 90

# Amazon's own SKUs for two IFAK colourways, from config/ithrive/listings.yaml.
# They cannot be derived from the internal names and are only ever data.
BLACK = "05-MN0Y-QNA3"  # IFAK-CAT-BLACK on FBA
GREEN = "H1-N3HD-QGG4"  # IFAK-CAT-GREEN on FBM
# The rest of the family silenced, so the ratio under test is the only one.
QUIET = dict.fromkeys(
    (
        "EV-HTQ6-X3U4",
        "EA-OASB-I658",
        "9J-D3HQ-S3Y4",
        "BZ-V99M-ZKRI",
        "FB-4Z14-IX1U",
        "TI-EGGD-FYKZ",
    ),
    0,
)


@pytest.fixture(scope="module")
def config() -> LoadedConfig:
    loaded, _ = load_config(REPO / "config", "ithrive")
    return loaded


def _run(config: LoadedConfig, sales: dict[str, int]) -> ReplenishmentResult:
    """The sample run with some listings' sales rewritten."""
    inventory = VeeqoFixtureClient(fixture_dir=SAMPLE)
    orders = GmailFixtureClient(fixture_dir=SAMPLE)
    velocity = inventory.read_velocity(WINDOW)
    for sku, units in sales.items():
        velocity[sku] = SalesVelocity(sku=sku, units_sold=units, window_days=WINDOW)
    return ReplenishmentCalculator(
        config=config,
        stock=inventory.read_inventory(),
        velocity=velocity,
        inbound=inventory.read_fba_inbound(),
        on_order=orders.read_order_signals().on_order,
        historical_velocity=inventory.read_velocity_history(),
    ).calculate()


# --- the rule itself -------------------------------------------------------


def test_the_split_follows_the_ratio_of_the_two_demands() -> None:
    """§7.1's worked example: 28 units, demand 30 against 10, so 21 and 7."""
    assert _split_by_demand([30, 10], 28) == [21, 7]


def test_three_to_one_stays_three_to_one_at_other_quantities() -> None:
    for send, expected in ((4, [3, 1]), (40, [30, 10]), (100, [75, 25])):
        assert _split_by_demand([30, 10], send) == expected


def test_a_faster_colourway_never_receives_less_than_a_slower_one() -> None:
    shares = _split_by_demand([97, 41, 41, 3], 37)
    assert shares == sorted(shares, reverse=True)
    assert shares[1] == shares[2], "equal demand, equal share"


def test_no_unit_is_created_or_lost_however_awkward_the_ratio() -> None:
    """The rounding matters: three ways to split 100 by thirds is 34/33/33,
    and a version that rounded each share down would ship 99."""
    for demands, send in (([1, 1, 1], 100), ([7, 11, 13], 29), ([5, 4], 3)):
        assert sum(_split_by_demand(demands, send)) == send


def test_a_family_with_no_demand_anywhere_still_ships_what_it_was_given() -> None:
    """There is no ratio to follow, and dropping the units silently would
    be the worse answer of the two."""
    assert _split_by_demand([0, 0], 12) == [12, 0]
    assert _split_by_demand([30, 10], 0) == [0, 0]


# --- and the rule as the calculator applies it -----------------------------


def test_two_colourways_at_different_velocities_split_the_send_by_demand(
    config: LoadedConfig,
) -> None:
    """End to end, on the real IFAK family: give black three times green's
    sales and black must take three times green's share of the shipment."""
    result = _run(config, {**QUIET, BLACK: 300, GREEN: 100})
    family = result.kit("IFAK-CAT")
    assert family.allocation is not None
    send = family.allocation.fba_send
    assert send > 0, "the case is only meaningful when something is being sent"

    shares = {member.kit_group: member.fba_send_share for member in family.members}
    demands = {member.kit_group: member.demand_units for member in family.members}

    # Hand-checkable: 164 against 55 is three to one, and 24 divides 18 / 6.
    assert (demands["IFAK-CAT-BLACK"], demands["IFAK-CAT-GREEN"]) == (164, 55)
    assert send == 24
    assert shares["IFAK-CAT-BLACK"] == 18
    assert shares["IFAK-CAT-GREEN"] == 6
    assert shares["IFAK-CAT-COYOTE"] == 0
    assert shares["IFAK-CAT-MULTICAM"] == 0
    assert sum(shares.values()) == send


def test_changing_the_velocities_changes_the_split_and_nothing_else(
    config: LoadedConfig,
) -> None:
    """A test that gives the same answer whatever the sales is not a test.

    Equal sales split evenly; widening the gap moves units from the slower
    colourway to the faster one, and the shipment stays the same size.
    """

    def shares(black: int, green: int) -> tuple[int, int, int]:
        family = _run(config, {**QUIET, BLACK: black, GREEN: green}).kit("IFAK-CAT")
        assert family.allocation is not None
        by_group = {member.kit_group: member.fba_send_share for member in family.members}
        return (
            by_group["IFAK-CAT-BLACK"],
            by_group["IFAK-CAT-GREEN"],
            family.allocation.fba_send,
        )

    assert shares(200, 200) == (4, 4, 8), "equal demand, equal share"
    black_leads, green_trails, send = shares(400, 100)
    assert (black_leads, green_trails, send) == (19, 5, 24)
    assert black_leads + green_trails == send
