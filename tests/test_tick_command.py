"""`shannon tick`: what a timer is allowed to start, and what it is not.

The timer exists so the Monday report arrives without anyone remembering
to ask for it. Filling a supplier's cart stays a decision Zach makes that
week, so the last test here is the important one: no schedule, no hour of
the day, and no flag on this command reaches a cart.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent_org import cli
from agent_org.cli import main
from agent_org.runtime.worker import RunAlreadyDone, RunSummary
from agent_org.tasks.queue import Task, TaskState

GOLDEN_CONFIG = Path(__file__).parent / "fixtures" / "golden" / "config"

# Sets the moment tick believes it is.
Clock = Callable[[datetime], None]

# The golden entity's schedule is "cron: 0 6 * * MON", read in
# America/New_York. Being due lasts the rest of the week, so within one
# week only the hours before Monday 06:00 are not due.
EASTERN = ZoneInfo("America/New_York")
MONDAY_TOO_EARLY = datetime(2026, 9, 7, 5, 30, tzinfo=EASTERN)
MONDAY_MORNING = datetime(2026, 9, 7, 7, 0, tzinfo=EASTERN)
WEDNESDAY = datetime(2026, 9, 9, 10, 0, tzinfo=EASTERN)
SUNDAY_NIGHT = datetime(2026, 9, 13, 23, 0, tzinfo=EASTERN)


class _NoConnection:
    """Enough of a connection to commit nothing."""

    def commit(self) -> None:
        return None


@contextmanager
def _no_database(*args: object, **kwargs: object) -> Iterator[_NoConnection]:
    """These tests are about what tick decides, not about storage."""
    yield _NoConnection()


def _summary() -> RunSummary:
    task = Task(
        id="task-1",
        entity_id="ithrive",
        kind="shannon_replenishment",
        schedule_slot="2026-W37",
        state=TaskState.SUCCEEDED,
        attempts=1,
        max_attempts=1,
        payload={},
        error=None,
    )
    return RunSummary(task=task, outcome=None, error=None)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Set the moment tick believes it is."""

    def at(moment: datetime) -> None:
        monkeypatch.setattr(cli, "_local_now", lambda config: moment)

    return at


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record every replenishment run tick starts, and start none."""
    started: list[dict[str, object]] = []

    def record(**kwargs: object) -> RunSummary:
        started.append(kwargs)
        return _summary()

    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/nothing")
    monkeypatch.setattr(cli, "run_replenishment", record)
    monkeypatch.setattr(cli, "connect", _no_database)
    monkeypatch.setattr(cli, "entity_session", _no_database)
    return started


def _tick(*extra: str) -> int:
    return main(["--config-root", str(GOLDEN_CONFIG), "tick", *extra])


def test_an_hour_with_nothing_due_runs_nothing_and_is_not_a_failure(
    clock: Clock, runs: list[dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Half past five on the Monday: not yet, and not a fault either."""
    clock(MONDAY_TOO_EARLY)
    code = _tick()
    assert code == 0
    assert runs == []
    assert "Nothing is due" in capsys.readouterr().out


def test_monday_morning_runs_the_week(
    clock: Clock, runs: list[dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    clock(MONDAY_MORNING)
    code = _tick("--no-email")
    assert code == 0
    assert len(runs) == 1
    assert "Report written to" in capsys.readouterr().out


@pytest.mark.parametrize("moment", [WEDNESDAY, SUNDAY_NIGHT])
def test_a_monday_the_machine_spent_switched_off_is_picked_up_later_that_week(
    clock: Clock, runs: list[dict[str, object]], moment: datetime
) -> None:
    """Due lasts the week, so a later boot still gets the report out."""
    clock(moment)
    assert _tick("--no-email") == 0
    assert len(runs) == 1


def test_the_second_tick_of_the_week_is_a_quiet_no_op(
    clock: Clock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asked hourly all week, the week's run still happens once.

    The guard is the task slot in the database, which raises. Every hour
    after the first therefore ends in an exit code the timer reads as
    success — a failed unit every hour would train Zach to ignore it.
    """

    def already_done(**kwargs: object) -> RunSummary:
        raise RunAlreadyDone("This week's run is already done (2026-W37).")

    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/nothing")
    monkeypatch.setattr(cli, "run_replenishment", already_done)
    monkeypatch.setattr(cli, "connect", _no_database)
    monkeypatch.setattr(cli, "entity_session", _no_database)
    clock(WEDNESDAY)
    assert _tick("--no-email") == 0
    assert "already done" in capsys.readouterr().out


def test_the_same_week_typed_by_hand_still_complains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shannon run` is someone expecting a report, so silence is wrong."""

    def already_done(**kwargs: object) -> RunSummary:
        raise RunAlreadyDone("This week's run is already done (2026-W37).")

    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/nothing")
    monkeypatch.setattr(cli, "run_replenishment", already_done)
    monkeypatch.setattr(cli, "connect", _no_database)
    monkeypatch.setattr(cli, "entity_session", _no_database)
    assert main(["--config-root", str(GOLDEN_CONFIG), "run", "--no-email"]) == 1


def test_an_unattended_run_reads_the_live_accounts_not_last_month_s_exports(
    clock: Clock, runs: list[dict[str, object]]
) -> None:
    """A timed report full of saved numbers would be worse than none."""
    clock(MONDAY_MORNING)
    _tick("--no-email")
    assert runs[0]["fixtures"] is None


def test_the_schedule_is_read_in_the_business_s_own_timezone(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, object]]
) -> None:
    """06:00 means six where the warehouse is, not six UTC.

    At 06:30 Eastern on the Monday it is already 10:30 UTC, and at 04:00
    Eastern it is 08:00 UTC: read in UTC, the first would be early and
    the second late.
    """
    monkeypatch.setattr(
        cli, "_local_now", lambda config: datetime(2026, 9, 7, 8, 0, tzinfo=UTC).astimezone(EASTERN)
    )
    assert _tick("--no-email") == 0
    assert runs == []


def test_a_timer_cannot_stage_a_cart(
    clock: Clock, runs: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one that matters: no path from the timer to a supplier's cart.

    Every way a cart could be reached from this process is booby-trapped,
    and then the busiest possible tick — the one that does run the week —
    is taken. Nothing goes off.
    """

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("the timer reached cart-staging code")

    monkeypatch.setattr(cli, "stage_supplier_cart", boom)
    monkeypatch.setattr(cli, "deliver_staging_report", boom)
    monkeypatch.setattr(cli, "cmd_stage", boom)
    monkeypatch.setattr("agent_org.runtime.staging.stage_supplier_cart", boom)
    monkeypatch.setattr("agent_org.integrations.nar.NarCartClient", boom)
    monkeypatch.setattr("agent_org.integrations.dynarex.DynarexPortalCart", boom)

    clock(MONDAY_MORNING)
    assert _tick("--no-email") == 0
    assert len(runs) == 1


def test_tick_names_no_staging_function_at_all() -> None:
    """Belt and braces: the source of the timer's path mentions no cart.

    The test above proves nothing was called on one run; this one holds
    the shape of the code, so a later branch that only stages on some
    Mondays is caught too.
    """
    source = textwrap.dedent(inspect.getsource(cli.cmd_tick) + inspect.getsource(cli._run_the_week))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert called
    assert not [name for name in called if "stage" in name or "cart" in name]
