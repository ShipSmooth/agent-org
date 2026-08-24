"""`shannon validate-config` — the command that makes Zach's own parts list safe.

Every message names a file and a line, in plain English. No stack traces,
ever: an invalid config is an expected outcome, not a crash.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_org.shannon.config_model import EntityConfig, load_entity_config
from agent_org.shannon.validate import validate

REPO = Path(__file__).resolve().parents[1]
COMMITTED_CONFIG = REPO / "config"


@pytest.fixture(scope="module")
def committed_issues() -> list[str]:
    cfg = load_entity_config(COMMITTED_CONFIG, "ithrive")
    return [i.render() for i in validate(cfg)]


def test_it_catches_the_dangling_instruction_card_reference(committed_issues: list[str]) -> None:
    """The deliberate violation: own_printed / CARD-TODO (parking lot PL-4)."""
    hits = [m for m in committed_issues if "CARD-TODO" in m]
    assert hits
    assert all(m.startswith("ERROR") for m in hits)
    assert all("boms.yaml:" in m for m in hits)


def test_it_catches_the_todo_fba_aliases(committed_issues: list[str]) -> None:
    """The other deliberate violation: TODO channel aliases (parking lot PL-8)."""
    hits = [m for m in committed_issues if "'fba' listing" in m and m.startswith("ERROR")]
    assert hits


def test_every_message_names_a_file_and_a_line(committed_issues: list[str]) -> None:
    assert committed_issues
    for message in committed_issues:
        assert ".yaml:" in message
        location = message.split(" ", 1)[1].split(":")[1]
        assert location.isdigit()


def test_a_pending_supplier_on_a_purchasable_component_is_an_error(
    committed_issues: list[str],
) -> None:
    assert any("pending supplier" in m and m.startswith("ERROR") for m in committed_issues)


def test_the_golden_config_passes_cleanly(golden_cfg: EntityConfig) -> None:
    assert [i.render() for i in validate(golden_cfg) if i.level == "error"] == []


def _issues_for(tmp_path: Path, golden_config_dir: Path, edit: tuple[str, str]) -> list[str]:
    """Copy the golden config, apply one textual change, and validate it."""
    for source in golden_config_dir.rglob("*.yaml"):
        target = tmp_path / source.relative_to(golden_config_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        target.write_text(text.replace(edit[0], edit[1]), encoding="utf-8")
    return [i.render() for i in validate(load_entity_config(tmp_path, "ithrive"))]


def test_an_asin_that_is_not_ten_characters_is_an_error(
    tmp_path: Path, golden_config_dir: Path
) -> None:
    issues = _issues_for(tmp_path, golden_config_dir, ("B0SALES30A", "B0SALES30"))
    assert any("10" in m and m.startswith("ERROR") for m in issues)


def test_a_bom_line_naming_an_unconfigured_channel_is_an_error(
    tmp_path: Path, golden_config_dir: Path
) -> None:
    issues = _issues_for(tmp_path, golden_config_dir, ("channels: [fba]", "channels: [tiktok]"))
    assert any("tiktok" in m and m.startswith("ERROR") for m in issues)


def test_a_bom_line_pointing_at_no_component_is_an_error(
    tmp_path: Path, golden_config_dir: Path
) -> None:
    issues = _issues_for(
        tmp_path,
        golden_config_dir,
        (
            '- {supplier: dynarex, part: "3161", qty: 1}',
            '- {supplier: dynarex, part: "NOPE-1", qty: 1}',
        ),
    )
    assert any("NOPE-1" in m and m.startswith("ERROR") for m in issues)


def test_a_notification_role_with_no_address_is_an_error(
    tmp_path: Path, golden_config_dir: Path
) -> None:
    """A report that silently goes nowhere is worse than a refused run."""
    issues = _issues_for(tmp_path, golden_config_dir, ("recipients: [zach]", "recipients: [angie]"))
    assert any("angie" in m and m.startswith("ERROR") for m in issues)


def test_a_reorder_point_above_its_target_is_a_warning_not_an_error(
    tmp_path: Path, golden_config_dir: Path
) -> None:
    issues = _issues_for(
        tmp_path,
        golden_config_dir,
        ("reorder_point: 100, reorder_target: 400", "reorder_point: 900, reorder_target: 400"),
    )
    assert any("reorder_point" in m and m.startswith("WARNING") for m in issues)
    assert not any("reorder_point" in m and m.startswith("ERROR") for m in issues)


def _run_cli(config_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_org.cli", "validate-config", "--config", str(config_dir)],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )


def test_the_command_exits_nonzero_on_the_committed_config_without_a_stack_trace() -> None:
    result = _run_cli(COMMITTED_CONFIG)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Traceback" not in output
    assert "bom_version:" in output
    assert "CARD-TODO" in output


def test_the_command_exits_zero_on_a_valid_config(golden_config_dir: Path) -> None:
    result = _run_cli(golden_config_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bom_version:" in result.stdout


def test_a_missing_config_directory_is_explained_not_crashed(tmp_path: Path) -> None:
    result = _run_cli(tmp_path / "nowhere")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Traceback" not in output
    assert "nowhere" in output
