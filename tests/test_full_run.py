"""A full fixture run: reads, arithmetic, a readable report file, and the
same text stored in the database. Nothing is sent anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from agent_org.runtime.wiring import build_broker
from agent_org.scheduler.schedule import WEEKLY_KIND, next_run_at, slot_for, tick
from agent_org.shannon.calculator import RunStopped
from agent_org.shannon.config_model import EntityConfig
from agent_org.shannon.run import run_replenishment
from agent_org.tenancy.session import entity_session

CONFIG = Path(__file__).resolve().parents[1] / "config"
SLOT = "2026-W34"


@pytest.fixture
def report(
    clean_db: psycopg.Connection,
    golden_cfg: EntityConfig,
    golden_config_dir: Path,
    golden_data_dir: Path,
    tmp_path: Path,
) -> tuple[str, str]:
    """Runs Shannon once; returns (report text, file path)."""
    outcome = run_replenishment(
        clean_db,
        build_broker(golden_cfg, CONFIG),
        config_dir=golden_config_dir,
        entity_id="ithrive",
        fixtures_dir=golden_data_dir,
        out_dir=tmp_path / "reports",
        schedule_slot=SLOT,
    )
    return Path(outcome.report_path).read_text(encoding="utf-8"), outcome.report_path


def test_the_run_writes_a_report_file(report: tuple[str, str]) -> None:
    text, path = report
    assert Path(path).exists()
    assert text.startswith("# Shannon")


def test_the_report_shows_every_intermediate_number_for_a_line(report: tuple[str, str]) -> None:
    """Zach checks the arithmetic by hand: raw → MOQ → nearest 5 → packs → units."""
    text = report[0]
    line = next(row for row in text.splitlines() if "nar/ZZ-0034" in row and "|" in row)
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert "127" in cells  # raw net requirement
    assert "130" in cells  # after MOQ and nearest-5
    assert "65" in cells  # purchase units (two-packs)


def test_the_report_carries_the_parameters_and_bom_version(report: tuple[str, str]) -> None:
    text = report[0]
    assert "BOM version: 2026-08-20-golden" in text
    assert "cover_target_weeks: 7 (inclusive of lead time)" in text
    assert "velocity_window_days: 90" in text


def test_the_report_carries_the_parking_lot_gaps_and_builds(report: tuple[str, str]) -> None:
    text = report[0]
    for heading in ("Parking lot", "Gap list", "Build recommendations", "Config diff"):
        assert heading.lower() in text.lower(), heading


def test_the_report_names_the_limiting_component_for_a_blocked_build(
    report: tuple[str, str],
) -> None:
    assert "IFAK-CAT-COYOTE-bag" in report[0]


def test_the_database_holds_exactly_the_same_report(
    clean_db: psycopg.Connection, report: tuple[str, str]
) -> None:
    text, path = report
    with entity_session(clean_db, "ithrive"):
        row = clean_db.execute(
            "SELECT content, file_path, schedule_slot FROM reports ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == text
    assert row[1] == path
    assert row[2] == SLOT


def test_the_run_is_recorded_as_succeeded_with_a_transcript(
    clean_db: psycopg.Connection, report: tuple[str, str]
) -> None:
    with entity_session(clean_db, "ithrive"):
        task = clean_db.execute(
            "SELECT state FROM tasks WHERE kind = %s AND schedule_slot = %s", (WEEKLY_KIND, SLOT)
        ).fetchone()
        run = clean_db.execute(
            "SELECT agent_kind, step_count FROM agent_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert task == ("SUCCEEDED",)
    assert run is not None
    assert run[0] == "shannon"
    assert run[1] >= 5


def test_running_the_same_slot_twice_refuses_rather_than_duplicating(
    clean_db: psycopg.Connection,
    golden_cfg: EntityConfig,
    golden_config_dir: Path,
    golden_data_dir: Path,
    report: tuple[str, str],
    tmp_path: Path,
) -> None:
    with pytest.raises(RunStopped):
        run_replenishment(
            clean_db,
            build_broker(golden_cfg, CONFIG),
            config_dir=golden_config_dir,
            entity_id="ithrive",
            fixtures_dir=golden_data_dir,
            out_dir=tmp_path / "reports",
            schedule_slot=SLOT,
        )


def test_a_broken_gmail_fixture_stops_the_run_and_records_the_failure(
    clean_db: psycopg.Connection,
    golden_cfg: EntityConfig,
    golden_config_dir: Path,
    golden_data_dir: Path,
    tmp_path: Path,
) -> None:
    """Double-ordering is the expensive failure: no signal means no run."""
    fixtures = tmp_path / "data"
    fixtures.mkdir()
    for source in golden_data_dir.glob("veeqo_*.json"):
        (fixtures / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(Exception, match="mailbox"):
        run_replenishment(
            clean_db,
            build_broker(golden_cfg, CONFIG),
            config_dir=golden_config_dir,
            entity_id="ithrive",
            fixtures_dir=fixtures,
            out_dir=tmp_path / "reports",
            schedule_slot="2026-W35",
        )
    with entity_session(clean_db, "ithrive"):
        row = clean_db.execute(
            "SELECT state, error FROM tasks WHERE schedule_slot = '2026-W35'"
        ).fetchone()
    assert row is not None
    assert row[0] == "FAILED"
    assert "mailbox" in str(row[1])


def test_the_scheduler_enqueues_one_run_a_week(clean_db: psycopg.Connection) -> None:
    monday = datetime(2026, 8, 24, 11, 0, tzinfo=UTC)  # 07:00 New York
    first = tick(clean_db, "ithrive", "America/New_York", monday)
    assert first is not None
    assert tick(clean_db, "ithrive", "America/New_York", monday) is None  # same slot, no-op


def test_the_scheduler_does_nothing_before_monday_six(clean_db: psycopg.Connection) -> None:
    monday_early = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)  # 04:00 New York
    assert tick(clean_db, "ithrive", "America/New_York", monday_early) is None


def test_a_missed_monday_is_still_run_later_in_the_week(clean_db: psycopg.Connection) -> None:
    """A scheduler that was off on Monday catches up, once, for that week."""
    wednesday = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
    assert tick(clean_db, "ithrive", "America/New_York", wednesday) is not None
    assert tick(clean_db, "ithrive", "America/New_York", wednesday) is None


def test_the_next_run_is_monday_at_six_local() -> None:
    now = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)  # a Wednesday
    nxt = next_run_at(now, "America/New_York")
    assert (nxt.weekday(), nxt.hour) == (0, 6)
    assert nxt > now


def test_a_slot_is_named_by_its_iso_week() -> None:
    assert slot_for(datetime(2026, 8, 24, 6, 0, tzinfo=UTC)) == "2026-W35"


def test_a_manual_run_never_steals_the_scheduler_s_queued_week(
    clean_db: psycopg.Connection,
    golden_cfg: EntityConfig,
    golden_config_dir: Path,
    golden_data_dir: Path,
    tmp_path: Path,
) -> None:
    """The weekly task sits queued; a manual run does its own slot only."""
    monday = datetime(2026, 8, 24, 11, 0, tzinfo=UTC)
    weekly = tick(clean_db, "ithrive", "America/New_York", monday)
    assert weekly is not None

    outcome = run_replenishment(
        clean_db,
        build_broker(golden_cfg, CONFIG),
        config_dir=golden_config_dir,
        entity_id="ithrive",
        fixtures_dir=golden_data_dir,
        out_dir=tmp_path / "reports",
        schedule_slot="2026-W35-manual",
    )
    assert outcome.report_path.endswith("2026-W35-manual.md")
    with entity_session(clean_db, "ithrive"):
        state = clean_db.execute("SELECT state FROM tasks WHERE id = %s", (weekly,)).fetchone()
    assert state == ("QUEUED",)
