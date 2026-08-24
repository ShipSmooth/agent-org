"""What "nothing to order" means, line by line.

The first real run described a pouch with nothing on hand and nothing on
order as "covered", and two hundred lines later called the same pouch the
most time-sensitive item on the list. Read on a Monday morning, the first
section is the one that gets skimmed and the wrong conclusion is the one
that gets drawn. Every test here holds one section to its own wording.
"""

from __future__ import annotations

import re

import pytest

from tests.test_end_to_end_run import live_config_report  # noqa: F401

ORCA_COYOTE = "ORCA-MOLLE-EMT-COYOTE"
COVERED = "Covered — stock meets the demand calculated for it:"
NO_DEMAND = "No demand this period"
CANNOT_ASSESS = "Cannot be assessed"
BLOCKING = "OUT OF STOCK AND STOPPING A BUILD"


def _section(report: str, heading: str) -> str:
    """One block of the report: from its heading to the blank line after it."""
    assert heading in report, heading
    body = report.split(heading, 1)[1]
    return body.split("\n\n", 1)[0]


@pytest.fixture
def quiet(live_config_report: str) -> str:  # noqa: F811
    return live_config_report.split("NOTHING TO ORDER THIS WEEK", 1)[1].split("NOT STOCKED", 1)[0]


def test_a_zero_stock_build_blocker_is_never_called_covered(
    live_config_report: str,  # noqa: F811
    quiet: str,
) -> None:
    """The Orca pouch from Zach's first run, exactly.

    It is at zero, and the build section says a colourway cannot be
    assembled without it. Whatever else the arithmetic concludes, no
    wording anywhere may present it as fine.
    """
    assert ORCA_COYOTE in live_config_report
    assert "IFAK-CAT-COYOTE" in live_config_report

    covered = _section(quiet, COVERED)
    assert ORCA_COYOTE not in covered
    assert "Coyote" not in covered

    blocked = _section(quiet, BLOCKING)
    assert ORCA_COYOTE in blocked
    assert "IFAK-CAT-COYOTE cannot be assembled without it" in blocked
    # And the word itself appears nowhere on that part's lines.
    for line in live_config_report.splitlines():
        if ORCA_COYOTE in line:
            assert "covered" not in line.lower(), line


def test_every_covered_line_names_the_demand_it_is_covered_against(quiet: str) -> None:
    """ "Covered" is a comparison, and a comparison with one side missing is
    an assertion. Each line prints the figure it was judged against."""
    covered = _section(quiet, COVERED)
    reasons = [line.strip() for line in covered.splitlines() if line.strip().startswith("covered")]
    assert reasons
    for reason in reasons:
        assert re.search(r"\d", reason), reason
        assert "demand of" in reason or "against a reorder point of" in reason, reason


def test_a_part_with_no_threshold_is_reported_as_unassessable(quiet: str) -> None:
    """A reorder-point part with no reorder point has no line to be below.
    Twelve of them used to sit silently under "covered"."""
    section = _section(quiet, CANNOT_ASSESS)
    assert "no reorder point is set" in section
    assert "nothing to judge it against" in section


def test_a_line_with_no_demand_says_why_it_had_none(quiet: str) -> None:
    """Zero is an answer, but only with its reason attached: nothing sold,
    no kit consumes it, or a listing that is down."""
    section = _section(quiet, NO_DEMAND)
    reasons = [
        line.strip() for line in section.splitlines() if line.strip().startswith("no demand")
    ]
    assert reasons
    for reason in reasons:
        assert "—" in reason, reason
        assert len(reason.split("—", 1)[1].split()) >= 4, reason


def test_a_blocking_part_is_named_once_with_the_colourways_it_stops(
    live_config_report: str,  # noqa: F811
) -> None:
    """The run Zach read named one dressing four times in one sentence,
    once per colourway. Name the part once, then say what it stops."""
    warnings = live_config_report.split("WARNINGS", 1)[1]
    for line in warnings.splitlines():
        if "can be assembled from stock on hand" not in line:
            continue
        parts = re.findall(r"\(([a-z_]+/[\w.-]+)\)", line)
        assert len(parts) == len(set(parts)), line
        if len(parts) > 1:
            assert "stops " in line, line


def test_the_parking_lot_lists_only_what_is_still_open(
    live_config_report: str,  # noqa: F811
) -> None:
    """Resolved items belong in a closed section at the end. The parking
    lot is what Zach still has to deal with."""
    section = live_config_report.split("PARKING LOT", 1)[1]
    live, _, closed = section.partition("  Closed — settled")
    assert "PL-1" in live
    assert "PL-4" not in live and "PL-8" not in live
    assert "PL-4" in closed and "PL-8" in closed
    assert "(resolved)" not in live


def test_the_open_items_are_in_numeric_order(
    live_config_report: str,  # noqa: F811
) -> None:
    """PL-2 before PL-10: sorted on the number, not on the text."""
    live = live_config_report.split("PARKING LOT", 1)[1].split("  Closed — settled", 1)[0]
    numbers = [int(match) for match in re.findall(r"^  PL-(\d+)", live, flags=re.MULTILINE)]
    assert numbers == sorted(numbers), numbers


def test_demand_prints_as_the_fraction_it_is_and_orders_whole_units(
    live_config_report: str,  # noqa: F811
) -> None:
    """148.63 is a real intermediate, not a display bug.

    Weekly velocity is units over 90 days and almost never lands whole.
    Rounding each kit's contribution up on the way would add a unit per kit
    per component; the single round-up happens at the net requirement, so
    what is printed as demand may be fractional and what is ordered never
    is. The document said "integers" throughout — the document was wrong
    and has been corrected.
    """
    demands = re.findall(r"demand of (\d+\.\d+) over the cover period", live_config_report)
    assert demands, "no line exercised the fractional path"
    ordered = re.findall(r"^      (\d+) → (\d+) → (\d+) →", live_config_report, flags=re.MULTILINE)
    assert ordered
    for row in ordered:
        assert all(figure.isdigit() for figure in row), row
