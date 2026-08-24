"""`shannon validate-config`, including the two deliberate violations.

config/ithrive/boms.yaml ships with a dangling `own_printed / CARD-TODO`
reference and unresolved `TODO` FBA aliases. Both are committed on purpose:
if this validator ever stops seeing them, it has stopped working.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_org.cli import main
from agent_org.config.loader import load_config
from agent_org.config.validate import validate

REPO = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO / "config"
GOLDEN_CONFIG = Path(__file__).parent / "fixtures" / "golden" / "config"


@pytest.fixture(scope="module")
def real_findings() -> list[str]:
    config, findings = load_config(REAL_CONFIG, "ithrive")
    result = validate(config, findings)
    return [finding.render() for finding in result.findings]


def test_dangling_card_todo_reference_is_caught(real_findings: list[str]) -> None:
    matches = [text for text in real_findings if "CARD-TODO" in text]
    assert matches, "the deliberate own_printed/CARD-TODO dangling reference was missed"


def test_todo_fba_aliases_are_caught(real_findings: list[str]) -> None:
    matches = [text for text in real_findings if "fba" in text and "TODO" in text]
    assert matches, "the deliberate TODO FBA aliases were missed"


def test_every_finding_names_a_file_and_a_line(real_findings: list[str]) -> None:
    for text in real_findings:
        first_line = text.splitlines()[0]
        assert ".yaml:" in first_line, first_line


def test_no_finding_reads_like_a_stack_trace(real_findings: list[str]) -> None:
    for text in real_findings:
        assert "Traceback" not in text
        assert "Error:" not in text
        assert ".py" not in text


def test_the_golden_config_is_clean() -> None:
    config, findings = load_config(GOLDEN_CONFIG, "ithrive")
    result = validate(config, findings)
    assert not result.errors, [finding.message for finding in result.errors]


def test_validate_config_command_fails_on_the_real_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--config-root", str(REAL_CONFIG), "validate-config"])
    out = capsys.readouterr().out
    assert code == 1
    assert "CARD-TODO" in out
    assert "Traceback" not in out


def test_validate_config_command_passes_on_the_golden_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--config-root", str(GOLDEN_CONFIG), "validate-config"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "No problems" in out
    assert "golden-2026-08-20" in out
