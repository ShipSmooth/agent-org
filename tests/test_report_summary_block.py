"""The first fifteen lines of the email.

The report says everything, which is why it is too long to act on before
the first coffee. AT A GLANCE goes at the top and says only what has to be
done and how much of it: the lines to order split by the route they go
out on, anything out of stock and stopping a build, and the size of the
warnings and parking lot. Nothing below it moved.

Every count here is checked against the section it summarises, in the same
rendered report, because a summary that quietly disagrees with its own
detail is worse than no summary at all.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from agent_org.config.loader import load_config
from agent_org.config.models import ComponentKey, LoadedConfig
from agent_org.integrations.reads import OrderSignals
from agent_org.shannon.calculator import (
    ManualProposal,
    ReplenishmentCalculator,
    ReplenishmentResult,
    Sufficiency,
)
from agent_org.shannon.report import ReportContext, render

REPO = Path(__file__).resolve().parents[1]
FRESH = date(2026, 9, 16)
COYOTE_POUCH = ComponentKey(supplier="orca_tactical", part="ORCA-MOLLE-EMT-COYOTE")


@pytest.fixture(scope="module")
def config() -> LoadedConfig:
    loaded, _ = load_config(REPO / "config", "ithrive")
    return loaded


def _result(
    config: LoadedConfig,
    proposals: dict[ComponentKey, ManualProposal] | None = None,
) -> ReplenishmentResult:
    return ReplenishmentCalculator(
        config=config,
        stock={},
        velocity={},
        inbound={},
        on_order={},
        today=FRESH,
        manual_proposals=proposals or {},
    ).calculate()


def _report(
    config: LoadedConfig,
    result: ReplenishmentResult,
    warnings: tuple[str, ...] = (),
    blocked: tuple[str, ...] = (),
) -> str:
    return render(
        result,
        config,
        ReportContext(
            entity_name=config.entity.legal_name,
            generated_at=datetime(2026, 9, 16, tzinfo=UTC),
            config_changes="",
            validation_warnings=warnings,
            order_signals=OrderSignals(on_order={}),
            data_sources=(),
            blocked=blocked,
        ),
    )


def _glance(report: str) -> str:
    assert "AT A GLANCE" in report
    return report.split("AT A GLANCE", 1)[1].split("PHASE 2", 1)[0]


@pytest.fixture(scope="module")
def report(config: LoadedConfig) -> str:
    return _report(config, _result(config))


def test_the_summary_is_the_first_thing_in_the_email(report: str) -> None:
    """Before the parameters, before the phase note, before everything.

    Zach reads this on a phone; whatever is at the top is what gets read,
    so what is at the top is what needs doing."""
    assert report.index("AT A GLANCE") < report.index("PHASE 2 — READ ONLY")
    assert report.index("AT A GLANCE") < report.index("PARAMETERS USED")


def test_the_full_report_is_still_underneath_it(report: str) -> None:
    """The summary replaces nothing. Every section Zach had on Monday is
    still there, in the order it was in."""
    sections = [
        "PARAMETERS USED",
        "WHERE THE NUMBERS CAME FROM",
        "WHAT TO ORDER",
        "NOTHING TO ORDER THIS WEEK",
        "NOT STOCKED — QUANTITY 0, ALWAYS",
        "KITS — BUILD RECOMMENDATIONS",
        "DEMAND SUPPRESSED",
        "FBA INBOUND PLAN",
        "GAP LIST — order these by hand",
        "BLOCKED — Shannon could not calculate these",
        "WARNINGS",
        "PARKING LOT",
    ]
    found = [report.index(section) for section in sections]
    assert all(section in report for section in sections)
    assert found == sorted(found)


def test_the_lines_to_order_are_split_by_the_route_they_go_out_on(
    config: LoadedConfig, report: str
) -> None:
    """The split is the point: a week is three errands, not one number, and
    each route is a different place Zach has to go. Counted off the same
    plans the WHAT TO ORDER section prints."""
    glance = _glance(report)
    expected: dict[str, int] = {}
    for plan in _result(config).components:
        if plan.order_units > 0:
            expected[plan.routing] = expected.get(plan.routing, 0) + 1
    assert expected, "the live parts list should have something to order"

    total = re.search(r"(\d+) lines? to order:", glance)
    assert total is not None, glance
    assert int(total.group(1)) == sum(expected.values())
    for routing, count in expected.items():
        assert re.search(rf"{routing}\s+{count}\b", glance), (routing, count, glance)


def test_the_route_totals_match_the_routes_printed_line_by_line(report: str) -> None:
    """Read off the rendered text rather than the objects, so the summary is
    checked against what Zach actually sees below it."""
    glance = _glance(report)
    ordering = report.split("WHAT TO ORDER", 1)[1].split("NOTHING TO ORDER THIS WEEK", 1)[0]
    printed: dict[str, int] = {}
    for routing in re.findall(r"route: (\S+)", ordering):
        printed[routing] = printed.get(routing, 0) + 1
    for routing, count in printed.items():
        assert re.search(rf"{routing}\s+{count}\b", glance), (routing, count, glance)


def test_the_warning_and_parking_lot_counts_match_the_sections(
    config: LoadedConfig, report: str
) -> None:
    """Two numbers that exist so Zach can decide whether to scroll."""
    glance = _glance(report)
    result = _result(config)
    warnings = len(result.warnings)
    open_parking = len([item for item in config.boms.parking_lot if not item.resolved]) + len(
        result.parking_lot_additions
    )
    assert f"{warnings} warnings." in glance
    assert f"{open_parking} open parking-lot questions." in glance
    # And the parking lot's closed items are not counted as open ones.
    assert any(item.resolved for item in config.boms.parking_lot)


def test_validation_warnings_are_counted_with_the_others(config: LoadedConfig) -> None:
    """The WARNINGS section prints both kinds under one heading, so the
    count above has to add up the same way."""
    result = _result(config)
    plain = _glance(_report(config, result))
    with_two_more = _glance(_report(config, result, warnings=("one", "two")))
    plain_count = int(re.search(r"(\d+) warnings?\.", plain).group(1))  # type: ignore[union-attr]
    assert f"{plain_count + 2} warnings." in with_two_more


def test_a_build_blocker_is_named_at_the_top_not_merely_counted(
    config: LoadedConfig,
) -> None:
    """The one urgent state that is invisible in the order list, because its
    order quantity is zero: already proposed against this same hand count,
    stock nil, and a colourway that cannot be assembled without it. It is
    named — part, product and reason — where a count alone would send Zach
    hunting for it two hundred lines down."""
    remembered = {
        COYOTE_POUCH: ManualProposal(
            key=COYOTE_POUCH,
            counted_on=date(2026, 8, 23),
            count=0,
            units=100,
            proposed_on=date(2026, 9, 7),
        )
    }
    result = _result(config, proposals=remembered)
    blocking = [
        plan for plan in result.components if plan.sufficiency is Sufficiency.BLOCKING_BUILD
    ]
    assert blocking, "the suppressed coyote pouch should be blocking a build"

    glance = _glance(_report(config, result))
    assert f"{len(blocking)} lines out of stock and stopping a build:" in glance
    assert "ORCA-MOLLE-EMT-COYOTE" in glance
    assert "IFAK-CAT-COYOTE cannot be assembled without it" in glance


def test_a_week_with_no_build_blocker_says_so_rather_than_going_quiet(
    config: LoadedConfig,
) -> None:
    """An absent section reads as a section that was forgotten. The good
    news is printed, on the quiet week this configuration never gives us:
    the same result with its blocked lines taken out."""
    result = _result(config)
    calm = replace(
        result,
        components=tuple(
            plan for plan in result.components if plan.sufficiency is not Sufficiency.BLOCKING_BUILD
        ),
    )
    assert "Nothing is out of stock and stopping a build." in _glance(_report(config, calm))


def test_lines_that_could_not_be_calculated_are_surfaced_at_the_top(
    config: LoadedConfig,
) -> None:
    """A blocked line is not an order and not a reassurance; it is work
    Shannon could not do, and it says how much of it there is."""
    glance = _glance(_report(config, _result(config), blocked=("nar/30-0052: no velocity",)))
    assert "1 line Shannon could not calculate at all — see BLOCKED." in glance


def test_the_summary_never_claims_anything_was_ordered(report: str) -> None:
    """Tier 0 has not moved. The summary is a list of recommendations, and
    the top of the email says so before Zach reaches the phase note."""
    assert "Nothing here is ordered or staged." in _glance(report)
