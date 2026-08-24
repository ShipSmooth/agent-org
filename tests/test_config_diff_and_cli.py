"""The config-change line at the top of every report, and the CLI itself."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_org.cli import main
from agent_org.config.models import LoadedConfig
from agent_org.shannon.config_diff import ConfigSnapshot, describe_changes

GOLDEN_CONFIG = Path(__file__).parent / "fixtures" / "golden" / "config"


def test_the_first_run_says_there_is_nothing_to_compare(golden_config: LoadedConfig) -> None:
    snapshot = ConfigSnapshot.of(golden_config)
    assert "first run on record" in describe_changes(snapshot, None)


def test_an_unchanged_configuration_says_so(golden_config: LoadedConfig) -> None:
    snapshot = ConfigSnapshot.of(golden_config)
    assert "No configuration changes" in describe_changes(snapshot, snapshot)


def test_a_changed_parameter_is_named_with_both_values(golden_config: LoadedConfig) -> None:
    current = ConfigSnapshot.of(golden_config)
    previous = replace(
        current,
        bom_version="golden-2026-01-01",
        parameters={**current.parameters, "cover_target_weeks": "6"},
        components=current.components[1:],
        kits=current.kits[1:],
    )
    sentence = describe_changes(current, previous)
    assert "golden-2026-01-01 → golden-2026-08-20" in sentence
    assert "cover_target_weeks 6 → 7" in sentence
    assert "components added" in sentence
    assert "kits added" in sentence


def test_a_snapshot_survives_a_round_trip_through_the_database(
    golden_config: LoadedConfig,
) -> None:
    snapshot = ConfigSnapshot.of(golden_config)
    assert ConfigSnapshot.from_dict(snapshot.as_dict()) == snapshot


def test_the_schedule_command_lists_what_is_wired(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--config-root", str(GOLDEN_CONFIG), "schedule"])
    out = capsys.readouterr().out
    assert code == 0
    assert "shannon_replenishment" in out
    assert "Runs are started by hand" in out


def test_a_command_needing_the_database_says_what_to_do_without_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Somewhere with no .env: the CLI reads one where it exists, and this
    # test is about the message when there is nothing to read.
    monkeypatch.chdir(tmp_path)
    code = main(["--config-root", str(GOLDEN_CONFIG), "migrate"])
    out = capsys.readouterr().out
    assert code == 1
    assert "DATABASE_URL is not set" in out
    assert "docker compose up" in out
    assert "Traceback" not in out
