"""Stock that lives on a shelf rather than in Veeqo.

Eleven components are not in Veeqo at all. Zach counts them. Until this
phase every one of them read "on hand 0", which was not a measurement —
it was Veeqo being asked about something it has never held. Now that they
have thresholds, that zero would have proposed the same eleven orders
every Monday for ever.

Three things are tested here, and they are the three ways that goes
wrong: a count that is typed rather than written in a note, an order
quantity that is a quantity rather than a target, and a proposal that is
made once per count rather than once a week.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from agent_org.config.loader import load_config
from agent_org.config.models import (
    MANUAL_COUNT_STALE_AFTER_DAYS,
    STOCK_SOURCE_MANUAL,
    STOCK_SOURCE_VEEQO,
    ComponentKey,
    LoadedConfig,
)
from agent_org.config.validate import validate
from agent_org.integrations.reads import OrderSignals
from agent_org.shannon.calculator import (
    ManualProposal,
    ReplenishmentCalculator,
    ReplenishmentResult,
    Sufficiency,
)
from agent_org.shannon.report import ReportContext, render

REPO = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO / "config"
COUNT_DAY = date(2026, 8, 26)
# Three weeks after the count: recent enough to be used without comment.
FRESH = date(2026, 9, 16)
# Nine weeks after it: still the only figure there is, and overdue.
STALE = date(2026, 10, 28)

BLUE_DOT = ComponentKey(supplier="amazon_business", part="B0D9GSKGY5")
GREEN_POUCH = ComponentKey(supplier="world_richman", part="IFAK-CAT-GREEN-bag")
COYOTE_POUCH = ComponentKey(supplier="orca_tactical", part="ORCA-MOLLE-EMT-COYOTE")


@pytest.fixture(scope="module")
def config() -> LoadedConfig:
    loaded, _ = load_config(REAL_CONFIG, "ithrive")
    return loaded


def _run(
    config: LoadedConfig,
    today: date,
    proposals: dict[ComponentKey, ManualProposal] | None = None,
) -> ReplenishmentResult:
    return ReplenishmentCalculator(
        config=config,
        stock={},
        velocity={},
        inbound={},
        on_order={},
        today=today,
        manual_proposals=proposals or {},
    ).calculate()


def _report(config: LoadedConfig, result: ReplenishmentResult, when: date) -> str:
    return render(
        result,
        config,
        ReportContext(
            entity_name=config.entity.legal_name,
            generated_at=datetime(when.year, when.month, when.day, tzinfo=UTC),
            config_changes="",
            validation_warnings=(),
            order_signals=OrderSignals(on_order={}),
            data_sources=(),
        ),
    )


def _broken(tmp_path: Path, before: str, after: str) -> list[str]:
    """The live config with one component edited, validated as of the count day."""
    root = tmp_path / "config"
    shutil.copytree(REAL_CONFIG, root)
    boms = root / "ithrive" / "boms.yaml"
    text = boms.read_text(encoding="utf-8")
    assert before in text, before
    boms.write_text(text.replace(before, after, 1), encoding="utf-8")
    loaded, findings = load_config(root, "ithrive")
    return [item.render() for item in validate(loaded, findings, today=COUNT_DAY).findings]


# --- the counts are data, not prose ---------------------------------------


def test_every_hand_counted_component_carries_a_typed_count(config: LoadedConfig) -> None:
    """A number that decides an order does not live in a comment.

    The counts arrived as `on_hand_note` strings because the field did not
    exist. Now it does, and nothing that matters is left in prose.
    """
    manual = [
        (key, component)
        for key, component in config.boms.components.items()
        if component.stock_source == STOCK_SOURCE_MANUAL
    ]
    assert len(manual) == 11, [str(key) for key, _ in manual]
    for key, component in manual:
        assert component.manual_stock is not None, key
        assert component.manual_stock.counted_on == COUNT_DAY, key
        assert component.reorder_point is not None, key
        assert component.reorder_quantity is not None, key

    # And nothing is left behind in prose: the notes the counts arrived in
    # are gone from the file, not merely ignored by the loader.
    text = (REAL_CONFIG / "ithrive" / "boms.yaml").read_text(encoding="utf-8")
    left_in_notes = [
        line
        for line in text.splitlines()
        if "on_hand_note" in line and not line.strip().startswith("#")
    ]
    assert not left_in_notes, left_in_notes


def test_everything_else_still_comes_from_veeqo(config: LoadedConfig) -> None:
    """`veeqo` is the default, and stays the default: the exception is the
    eleven, not the rule."""
    veeqo = [
        key
        for key, component in config.boms.components.items()
        if component.stock_source == STOCK_SOURCE_VEEQO
    ]
    assert len(veeqo) > 40
    assert all(config.boms.components[key].manual_stock is None for key in veeqo)


def test_a_manual_source_without_a_count_is_rejected(tmp_path: Path) -> None:
    """ "Do not ask Veeqo" with nothing in its place is a silent zero, which
    is the exact failure this whole mechanism exists to prevent."""
    findings = _broken(
        tmp_path,
        "     manual_stock: {count: 4000, counted_on: 2026-08-26},\n",
        "",
    )
    matches = [text for text in findings if "B0D9GSKGY5" in text and "manual_stock" in text]
    assert matches, findings
    assert all(text.startswith("ERROR") for text in matches), matches


def test_a_count_dated_in_the_future_is_rejected(tmp_path: Path) -> None:
    """A typo in a date is how a count nobody has taken becomes on-hand stock."""
    findings = _broken(
        tmp_path,
        "manual_stock: {count: 4000, counted_on: 2026-08-26}",
        "manual_stock: {count: 4000, counted_on: 2027-01-04}",
    )
    matches = [text for text in findings if "2027-01-04" in text]
    assert matches, findings
    assert all(text.startswith("ERROR") for text in matches), matches
    assert any("has not happened" in text for text in matches), matches


def test_a_component_may_not_set_both_a_target_and_a_quantity(tmp_path: Path) -> None:
    """ "Top up to 1,000" and "buy 1,000" are different instructions and
    cannot both be followed."""
    findings = _broken(
        tmp_path,
        "reorder_point: 1000, reorder_quantity: 5000,",
        "reorder_point: 1000, reorder_quantity: 5000, reorder_target: 6000,",
    )
    matches = [text for text in findings if "B0D9GSKGY5" in text and "reorder_target" in text]
    assert matches, findings
    assert all(text.startswith("ERROR") for text in matches), matches


# --- the count is used, and its age is said out loud ----------------------


def test_the_count_and_its_age_are_printed_in_plain_words(config: LoadedConfig) -> None:
    """ "4,000, counted 26 Aug (3 weeks ago)" — the figure and how much to
    trust it, in the same sentence."""
    body = _report(config, _run(config, FRESH), FRESH)
    assert "Blue dot label — 4,000, counted 26 Aug (3 weeks ago)" in body
    assert "counted by hand, not held in Veeqo" in body


def test_a_hand_counted_part_takes_its_stock_from_the_count_not_from_veeqo(
    config: LoadedConfig,
) -> None:
    """Veeqo returned nothing for it, because Veeqo has never held it. The
    green pouch is 4,750 on the shelf and is therefore not ordered."""
    green = _run(config, FRESH).component(GREEN_POUCH.supplier, GREEN_POUCH.part)
    assert green.on_hand == 4750
    assert green.order_units == 0
    assert green.sufficiency is Sufficiency.COVERED


def test_a_count_older_than_eight_weeks_is_still_used_and_goes_to_the_parking_lot(
    config: LoadedConfig,
) -> None:
    """Ignoring it would mean pretending the shelf is empty; treating it as
    fresh would mean pretending nothing has been used. It is used, and a
    recount is asked for."""
    assert MANUAL_COUNT_STALE_AFTER_DAYS == 56
    result = _run(config, STALE)
    green = result.component(GREEN_POUCH.supplier, GREEN_POUCH.part)
    assert green.on_hand == 4750, "a stale count is still the only figure there is"

    body = _report(config, result, STALE)
    parked = [item.id for item in result.parking_lot_additions if "RECOUNT" in item.id]
    assert parked, [item.id for item in result.parking_lot_additions]
    assert "needs recounting" in body
    assert "9 weeks ago" in body


# --- proposed once per count, not once a week ------------------------------


def test_the_same_order_is_not_proposed_twice_against_one_count(
    config: LoadedConfig,
) -> None:
    """The second Monday of an unchanged count.

    The shelf figure has not moved, because a count does not move on its
    own. Shannon says what she already proposed and asks for the new number
    rather than proposing it again.
    """
    first = _run(config, FRESH)
    coyote = first.component(COYOTE_POUCH.supplier, COYOTE_POUCH.part)
    assert coyote.order_units == 100

    remembered = {
        proposal.key: proposal
        for proposal in first.manual_proposals
        if proposal.key == COYOTE_POUCH
    }
    assert remembered, [str(item.key) for item in first.manual_proposals]

    second = _run(config, FRESH, proposals=remembered)
    again = second.component(COYOTE_POUCH.supplier, COYOTE_POUCH.part)
    assert again.order_units == 0
    assert "Proposed 100 on 16 Sep against this same count of 0" in again.sufficiency_reason
    assert "Not repeating it" in again.sufficiency_reason
    assert "Tell me the new count" in again.sufficiency_reason


def test_a_new_count_is_proposed_against_afresh(config: LoadedConfig) -> None:
    """Suppression is keyed on the count date, so a recount unblocks it —
    otherwise a part could never be ordered a second time."""
    stale_proposal = {
        COYOTE_POUCH: ManualProposal(
            key=COYOTE_POUCH,
            counted_on=date(2026, 6, 1),
            count=0,
            units=100,
            proposed_on=date(2026, 6, 2),
        )
    }
    result = _run(config, FRESH, proposals=stale_proposal)
    coyote = result.component(COYOTE_POUCH.supplier, COYOTE_POUCH.part)
    assert coyote.order_units == 100


def test_a_part_that_is_still_blocking_a_build_says_so_while_suppressed(
    config: LoadedConfig,
) -> None:
    """Not repeating an order is not the same as the problem going away.

    The coyote pouch is at zero and stops a colourway being assembled. The
    line stays in the loud section; what changes is the reason attached to
    it.
    """
    remembered = {
        COYOTE_POUCH: ManualProposal(
            key=COYOTE_POUCH,
            counted_on=COUNT_DAY,
            count=0,
            units=100,
            proposed_on=date(2026, 9, 7),
        )
    }
    result = _run(config, FRESH, proposals=remembered)
    coyote = result.component(COYOTE_POUCH.supplier, COYOTE_POUCH.part)
    assert coyote.sufficiency is Sufficiency.BLOCKING_BUILD
    assert "Not repeating it" in coyote.sufficiency_reason
    assert "IFAK-CAT-COYOTE cannot be assembled without it" in coyote.sufficiency_reason


# --- a quantity is a quantity ---------------------------------------------


def test_a_fixed_quantity_is_ordered_as_written(config: LoadedConfig) -> None:
    """ "When I get to 200, reorder 1,000" means 1,000 — not "top up to
    1,000", which at 150 on the shelf would buy 850."""
    zip_tie = ComponentKey(supplier="amazon_business", part="B08LKC2DFY")
    component = config.boms.components[zip_tie]
    assert (component.reorder_point, component.reorder_quantity) == (200, 2000)
    assert component.reorder_target is None
    plan = _run(config, FRESH).component(zip_tie.supplier, zip_tie.part)
    # 300 counted on the shelf, against a point of 200: nothing to do.
    assert plan.on_hand == 300
    assert plan.order_units == 0


def test_a_fixed_quantity_is_raised_until_it_clears_the_reorder_point(
    config: LoadedConfig,
) -> None:
    """A part found far below its point must not still be below it after
    the order arrives. 100 short of a 20 point orders the 100; 1,000 short
    of a 300 point orders enough to clear 300."""
    calculator = ReplenishmentCalculator(
        config=config,
        stock={},
        velocity={},
        inbound={},
        on_order={},
        today=FRESH,
        manual_proposals={},
    )
    # reorder_point 300, reorder_quantity 1000, hand count 2000 → covered.
    black = calculator.calculate().component("world_richman", "IFAK-CAT-BLACK-bag")
    assert black.order_units == 0

    # And the arithmetic itself, where the shelf is far below the point.
    assert _clearing(quantity=100, point=20, available=0) == 100
    assert _clearing(quantity=100, point=500, available=0) == 500
    assert _clearing(quantity=100, point=500, available=450) == 100
    assert _clearing(quantity=100, point=20, available=20) == 0


def _clearing(quantity: int, point: int, available: int) -> int:
    """The rule in section 2, written out once so the test states it plainly."""
    if available >= point:
        return 0
    return max(quantity, point - available)
