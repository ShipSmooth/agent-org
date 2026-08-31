"""Products Zach buys complete and resells as they come.

Forty-eight of them: NAR finished kits and NAR components he resells
unchanged. They are components because he buys them, but they are never
inside anything, so belonging to no kit is their normal state rather than
a missing kit line. Everything here is that one distinction, tested from
both sides — the flag must silence the warning for these, and must not
silence it for an ordinary part somebody forgot to attach.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_org.config.loader import load_config
from agent_org.config.models import ComponentClass, ComponentKey, LoadedConfig
from agent_org.config.validate import validate
from agent_org.shannon.calculator import moq_round, round_up_to

REPO = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO / "config"


def _findings(root: Path) -> list[str]:
    config, findings = load_config(root, "ithrive")
    return [finding.render() for finding in validate(config, findings).findings]


def _edited(tmp_path: Path, old: str, new: str) -> list[str]:
    root = tmp_path / "config"
    shutil.copytree(REAL_CONFIG, root)
    path = root / "ithrive" / "boms.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return _findings(root)


@pytest.fixture(scope="module")
def config() -> LoadedConfig:
    loaded, _ = load_config(REAL_CONFIG, "ithrive")
    return loaded


@pytest.fixture(scope="module")
def real_findings() -> list[str]:
    return _findings(REAL_CONFIG)


def test_the_resale_products_are_in_the_bom(config: LoadedConfig) -> None:
    resale = [key for key, item in config.boms.components.items() if item.resale_only]
    # The 42 originally supplied, plus the blue training tourniquet 30-0033,
    # which has always been resold standalone and is now marked as what it is,
    # plus the five off Zach's weekly list added on 30 Aug 2026.
    assert len(resale) == 48
    assert len(config.boms.components) == 89
    assert len(config.boms.kits) == 12
    # A sample of the SKUs Zach's existing weekly NAR procedure covers.
    for part in ("80-0167", "80-0439", "85-0834", "20-0040"):
        component = config.boms.components[ComponentKey("nar", part)]
        assert component.resale_only
        assert component.component_class is ComponentClass.FORECAST
        assert component.units_per_purchase_unit == 1


# Zach's working weekly NAR reorder list, which lived only in his head and in
# the operational Knowledge note. Seven of these were missing from the BOM
# entirely on 30 Aug 2026, so Shannon could never have ordered them and said
# nothing about it — a silent gap is the worst kind. Two of the seven turned
# out to be parts he does not buy, and are recorded as that decision.
ZACHS_WEEKLY_NAR_LIST = (
    "80-0494",
    "80-1034",
    "80-1667",
    "80-1703",
    "80-0107",
    "80-0027",
    "80-0542",
    "85-0417",
    "80-1612",
    "80-0947",
    "80-0465",
    "82-0075",
    "80-1049",
    "85-0008",
    "80-0439",
    "80-1067",
    "80-0901",
    "20-0040",
    "85-0177",
    "80-1490",
    "85-0180",
    "80-0573",
    "85-0834",
    "80-0167",
    "80-0452",
    "85-0404",
)
NOT_BOUGHT = ("80-0107", "80-0027")


def test_every_sku_on_zachs_weekly_nar_list_is_accounted_for(config: LoadedConfig) -> None:
    """Either Shannon can order it, or the file says why she does not."""
    boms = REAL_CONFIG / "ithrive" / "boms.yaml"
    text = boms.read_text(encoding="utf-8")
    for part in ZACHS_WEEKLY_NAR_LIST:
        if part in NOT_BOUGHT:
            assert ComponentKey("nar", part) not in config.boms.components, part
            assert f"#   {part}" in text, f"{part} is not bought, and nothing says so"
            continue
        component = config.boms.components[ComponentKey("nar", part)]
        assert component.resale_only, part
        assert component.component_class is ComponentClass.FORECAST, part
        assert component.units_per_purchase_unit == 1, part


def test_no_minimum_is_invented_for_them(config: LoadedConfig) -> None:
    """NAR's terms are known for the C-A-T and HyFin and for nothing else.

    An invented minimum on a $363 kit is a four-figure mistake, so these
    round to the nearest 5 and stop there.
    """
    for key, component in config.boms.components.items():
        if component.resale_only and key.part != "30-0033":
            assert component.moq_min == 0, key
            assert moq_round(7, component) == 7, key
            assert round_up_to(moq_round(7, component), 5) == 10, key


def test_a_resale_product_defaults_to_false_when_the_flag_is_absent(
    config: LoadedConfig,
) -> None:
    """Every ordinary part in the file says nothing about resale, and reads
    as false — the flag is opt-in, never inferred from a supplier name."""
    gauze = config.boms.components[ComponentKey("nar", "30-0052")]
    assert not gauze.resale_only


def test_belonging_to_no_kit_is_not_a_gap_for_them(real_findings: list[str]) -> None:
    unused = [text for text in real_findings if "is in no kit" in text or "no kit uses" in text]
    assert not unused, unused


def test_an_ordinary_part_in_no_kit_is_still_a_warning(tmp_path: Path) -> None:
    """The check is a good one and is kept: a component nobody assembles is
    usually a deleted kit line or a mistyped part number."""
    findings = _edited(
        tmp_path,
        "components:\n",
        "components:\n"
        '  - {supplier: nar, part: "99-0001", name: "Orphan part", class: forecast,\n'
        "     units_per_purchase_unit: 1}\n",
    )
    matches = [text for text in findings if "99-0001" in text and "no kit" in text]
    assert matches, findings
    assert all(text.startswith("WARNING") for text in matches), matches


def test_a_kit_line_naming_a_resale_product_is_rejected(tmp_path: Path) -> None:
    """One part cannot be both bought complete for resale and consumed by
    an assembly. Silently allowing it would explode demand for something
    that is never taken apart."""
    findings = _edited(
        tmp_path,
        '{supplier: nar, part: "30-0052", qty: 2}',
        '{supplier: nar, part: "80-0167", qty: 2}',
    )
    matches = [text for text in findings if "80-0167" in text and "resale_only" in text]
    assert matches, findings
    assert all(text.startswith("ERROR") for text in matches), matches


def test_the_forty_two_listing_warning_is_gone(real_findings: list[str]) -> None:
    """It used to name 42 products Shannon could see selling and could
    never order. They are modelled now, so the check has nothing to say."""
    assert not [text for text in real_findings if "the BOM does not describe" in text]


def test_a_genuinely_unmodelled_listing_still_warns(tmp_path: Path) -> None:
    """The check itself is kept — a listing with no component behind it is
    a real gap, and this proves the silence above is not the check dying."""
    root = tmp_path / "config"
    shutil.copytree(REAL_CONFIG, root)
    path = root / "ithrive" / "listings.yaml"
    text = path.read_text(encoding="utf-8")
    marker = "component_sales_asins:\n"
    assert marker in text
    path.write_text(
        text.replace(marker, marker + '  "99-9999": [B000000000]\n', 1), encoding="utf-8"
    )
    findings = _findings(root)
    matches = [text for text in findings if "the BOM does not describe" in text]
    assert matches, findings
    assert "99-9999" in matches[0]
