"""`shannon validate-config`.

Two halves. The live iThrive configuration must validate cleanly — Zach
runs against it, and a config that is knowingly broken teaches everyone to
ignore the output. The error paths are proved against
tests/fixtures/invalid, a config broken on purpose, so no test depends on a
mistake left in a production file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_org.cli import main
from agent_org.config.loader import load_config
from agent_org.config.models import ComponentKey, cover_target_for
from agent_org.config.validate import validate

REPO = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO / "config"
GOLDEN_CONFIG = Path(__file__).parent / "fixtures" / "golden" / "config"
INVALID_CONFIG = Path(__file__).parent / "fixtures" / "invalid" / "config"


def _findings(root: Path) -> list[str]:
    config, findings = load_config(root, "ithrive")
    result = validate(config, findings)
    return [finding.render() for finding in result.findings]


@pytest.fixture(scope="module")
def real_findings() -> list[str]:
    return _findings(REAL_CONFIG)


@pytest.fixture(scope="module")
def invalid_findings() -> list[str]:
    return _findings(INVALID_CONFIG)


def test_the_live_config_has_no_errors() -> None:
    config, findings = load_config(REAL_CONFIG, "ithrive")
    result = validate(config, findings)
    assert not result.errors, [finding.render() for finding in result.errors]


def test_the_fba_aliases_are_no_longer_a_gap(real_findings: list[str]) -> None:
    """PL-8 is closed: listings.yaml holds Amazon's own SKUs.

    Amazon's SKUs cannot be derived from Zach's, so they could only ever
    arrive as data. Now that they have, a kit whose BOM alias still says
    TODO is not missing anything, and saying so every week would train him
    to ignore the warnings that are real.
    """
    assert not [text for text in real_findings if "fba" in text and "TODO" in text]


def test_the_three_kits_with_no_amazon_listing_are_not_reported_as_a_gap(
    real_findings: list[str],
) -> None:
    """Structurally zero is a fact, not missing data (confirmed 24 Aug 2026)."""
    for kit_group in ("20-314", "20-315", "25-002"):
        assert not [text for text in real_findings if kit_group in text and "SKU" in text], (
            f"{kit_group} sells on Shopify and direct only; that is not a gap"
        )


def test_a_kit_listed_nowhere_active_is_reported_as_suppressed(
    real_findings: list[str],
) -> None:
    """25-010 is inactive on both channels: out of stock, so taken down."""
    matches = [text for text in real_findings if "25-010" in text and "suppressed" in text]
    assert matches, "25-010's inactive listings were not surfaced"
    assert all(text.startswith("WARNING") for text in matches), matches


def test_a_dangling_component_reference_is_caught(invalid_findings: list[str]) -> None:
    matches = [text for text in invalid_findings if "CARD-TODO" in text]
    assert matches, "the own_printed/CARD-TODO dangling reference was missed"
    assert all(text.startswith("ERROR") for text in matches), matches


def test_an_unknown_channel_and_a_malformed_asin_are_caught(
    invalid_findings: list[str],
) -> None:
    assert any("tiktok" in text for text in invalid_findings)
    assert any("exactly 10 letters and digits" in text for text in invalid_findings)


def test_a_lead_time_longer_than_the_cover_target_is_caught(
    invalid_findings: list[str],
) -> None:
    matches = [text for text in invalid_findings if "takes 9 weeks to deliver" in text]
    assert matches, "World Richman's 9-week lead time against a 7-week cover was missed"
    assert all(text.startswith("ERROR") for text in matches), matches


def _with_world_richman_cover(tmp_path: Path, cover: str | None) -> Path:
    """Copy the broken fixture, optionally giving World Richman a cover target."""
    root = tmp_path / "config"
    shutil.copytree(INVALID_CONFIG, root)
    boms = root / "ithrive" / "boms.yaml"
    text = boms.read_text(encoding="utf-8")
    if cover is not None:
        text = text.replace(
            "    lead_time_weeks: 9",
            f"    lead_time_weeks: 9\n    cover_target_weeks: {cover}",
        )
    boms.write_text(text, encoding="utf-8")
    return root


def test_a_supplier_cover_target_override_resolves(tmp_path: Path) -> None:
    # 9 weeks' lead time under a 7-week cover is a config-load failure; the
    # 13-week supplier override (9 + 4 buffer) is what makes it legal.
    too_short = _findings(_with_world_richman_cover(tmp_path / "seven", "7"))
    assert any("takes 9 weeks to deliver" in text for text in too_short)

    long_enough = _findings(_with_world_richman_cover(tmp_path / "thirteen", "13"))
    assert not any("takes 9 weeks to deliver" in text for text in long_enough)


def test_the_live_world_richman_override_is_the_one_in_use() -> None:
    config, _ = load_config(REAL_CONFIG, "ithrive")
    supplier = config.boms.suppliers["world_richman"]
    assert supplier.lead_time_weeks == 9
    assert supplier.cover_target_weeks == 13
    # A component with no figure of its own inherits its supplier's.
    pouch = config.boms.components[ComponentKey("world_richman", "IFAK-CAT-BLACK-bag")]
    assert pouch.cover_target_weeks is None
    assert cover_target_for(pouch, supplier, config.shannon.parameters.cover_target_weeks) == 13


def _edited_config(tmp_path: Path, old: str, new: str) -> list[str]:
    """The live config with one line changed — a mistake Zach could make."""
    root = tmp_path / "config"
    shutil.copytree(REAL_CONFIG, root)
    for path in root.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        if old in text:
            path.write_text(text.replace(old, new), encoding="utf-8")
    return _findings(root)


def test_a_reminder_sent_to_nobody_is_an_error(tmp_path: Path) -> None:
    """A report that silently goes nowhere is worse than a refused run."""
    findings = _edited_config(tmp_path, "recipients: [zach]", "recipients: [angie]")
    matches = [text for text in findings if "angie" in text]
    assert matches, "an unmapped recipient role was not reported"
    assert all(text.startswith("ERROR") for text in matches), matches


def test_a_reorder_point_above_its_target_is_a_warning_not_an_error(
    tmp_path: Path,
) -> None:
    """Wrong, and worth saying, but the other thirty-nine parts still add up."""
    findings = _edited_config(
        tmp_path,
        "reorder_point: 100, reorder_target: 400",
        "reorder_point: 900, reorder_target: 400",
    )
    matches = [text for text in findings if "900" in text and "400" in text]
    assert matches, "a reorder point above its target went unreported"
    assert all(text.startswith("WARNING") for text in matches), matches


def test_every_finding_names_a_file_and_a_line(
    real_findings: list[str], invalid_findings: list[str]
) -> None:
    for text in real_findings + invalid_findings:
        first_line = text.splitlines()[0]
        assert ".yaml:" in first_line, first_line
        assert first_line.rsplit(":", 1)[1].isdigit(), first_line


def test_no_finding_reads_like_a_stack_trace(
    real_findings: list[str], invalid_findings: list[str]
) -> None:
    for text in real_findings + invalid_findings:
        assert "Traceback" not in text
        assert "Error:" not in text
        assert ".py" not in text


def test_the_golden_config_is_clean() -> None:
    config, findings = load_config(GOLDEN_CONFIG, "ithrive")
    result = validate(config, findings)
    assert not result.errors, [finding.message for finding in result.errors]


def test_validate_config_command_passes_on_the_live_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--config-root", str(REAL_CONFIG), "validate-config"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "No problems" in out
    assert "2026-08-24" in out
    assert "Traceback" not in out


def test_validate_config_command_fails_on_the_broken_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--config-root", str(INVALID_CONFIG), "validate-config"])
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
