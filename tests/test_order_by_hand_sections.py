"""The two halves of the week's ordering, and the links in one of them.

Shannon stages NAR and nothing else: dynarex.com serves an image CAPTCHA,
which nobody is to automate past, and there is no Amazon integration at
all. So the email has to say, unmistakably, which lines are already in a
cart and which are Zach's own errand — and for the errand, take him to the
exact item's page rather than a search that could offer a neighbour first.

3161 is why the link rule is written the way it is: Dynarex's own search
for it also offers 33161 and 43161.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from agent_org.config.errors import Finding, Severity
from agent_org.config.loader import _product_url, load_config
from agent_org.config.models import Capability, ComponentKey, LoadedConfig
from agent_org.config.yamlsource import Loc, YamlMap
from agent_org.integrations.reads import OrderSignals, SalesVelocity
from agent_org.shannon.calculator import ReplenishmentCalculator, ReplenishmentResult
from agent_org.shannon.product_links import amazon_product_url, product_url
from agent_org.shannon.report import (
    ReportContext,
    _staging_is_authorised,
    by_hand_block,
    render,
    staged_block,
)

REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 16)
GAUZE = ComponentKey(supplier="dynarex", part="3161")
GLOVES = ComponentKey(supplier="amazon_business", part="B0FC56RYZ3")
BANDAGE = ComponentKey(supplier="dynarex", part="3681")


def _entry(url: str) -> YamlMap:
    entry = YamlMap({"product_url": url})
    entry.loc = Loc(file="boms.yaml", line=1)
    entry.key_locs = {}
    return entry


@pytest.fixture(scope="module")
def config() -> LoadedConfig:
    loaded, _ = load_config(REPO / "config", "ithrive")
    return loaded


def _result(config: LoadedConfig) -> ReplenishmentResult:
    return ReplenishmentCalculator(
        config=config,
        stock={},
        velocity={},
        inbound={},
        on_order={},
        today=TODAY,
        manual_proposals={},
    ).calculate()


@pytest.fixture(scope="module")
def report(config: LoadedConfig) -> str:
    return render(
        _result(config),
        config,
        ReportContext(
            entity_name=config.entity.legal_name,
            generated_at=datetime(2026, 9, 16, tzinfo=UTC),
            config_changes="",
            validation_warnings=(),
            order_signals=OrderSignals(on_order={}),
            data_sources=(),
        ),
    )


def _by_hand(report: str) -> str:
    return report.split("ORDER THESE BY HAND", 1)[1].split("NOTHING TO ORDER", 1)[0]


def test_only_nar_can_be_staged(config: LoadedConfig) -> None:
    """The capability is the whole safety mechanism: with stage_cart gone,
    the broker refuses a Dynarex or Amazon cart write however it is asked."""
    suppliers = config.boms.suppliers
    assert suppliers["nar"].can(Capability.STAGE_CART)
    assert not suppliers["dynarex"].can(Capability.STAGE_CART)
    assert not suppliers["amazon_business"].can(Capability.STAGE_CART)


def test_the_two_kinds_of_work_are_separate_sections(report: str) -> None:
    """A line Zach has to go and buy and a line already sitting in a cart
    are different errands; at a glance they must not read the same."""
    assert report.index("SHANNON STAGES THESE FOR YOU") < report.index("ORDER THESE BY HAND")
    assert "nothing below is in a cart" in report
    assert "Shannon has not staged, reserved or ordered any of these" in _by_hand(report)


def test_each_by_hand_supplier_gets_its_own_block(config: LoadedConfig, report: str) -> None:
    """One trip to dynarex.com, one to Amazon — not one interleaved list."""
    section = _by_hand(report)
    assert section.index("Dynarex Corporation") < section.index("Own printed inserts")
    assert "Amazon Business" in section
    for supplier in ("dynarex", "amazon_business"):
        name = config.boms.suppliers[supplier].name
        assert section.count(f"\n  {name}\n") == 1


def test_a_dynarex_line_carries_its_exact_product_page(report: str) -> None:
    """The link is to 3161's own page. Never product_search — that is the
    page that offers 33161 and 43161 alongside it."""
    section = _by_hand(report)
    assert (
        "https://dynarex.com/products/disposable-medical-supplies/"
        "general-advanced-wound-care/3161-krinkle-gauze-roll--sterile" in section
    )
    assert "product_search" not in section


def test_an_amazon_line_is_linked_by_its_own_asin(report: str) -> None:
    """The part number is the purchase ASIN, and /dp/<ASIN> is one listing."""
    assert "https://www.amazon.com/dp/B0FC56RYZ3" in _by_hand(report)


def test_every_by_hand_line_says_what_and_how_many(config: LoadedConfig) -> None:
    """Description, item code and quantity on every line, link or no link:
    the line has to be orderable by a human reading only this email."""
    result = _result(config)
    section = "\n".join(by_hand_block(result, config))
    for plan in result.components:
        if plan.order_units <= 0 or plan.routing != "gap_list":
            continue
        assert plan.name in section
        assert plan.key.part in section
        assert f"{plan.order_units} units needed" in section


def test_the_quantity_offered_is_packs_and_not_sellable_units(config: LoadedConfig) -> None:
    """A quantity box counts packs. Leading with the sellable count is how
    2000 bandages becomes 2000 cases of 240, so the pack count leads and
    the need it satisfies follows it."""
    result = _result(config)
    section = "\n".join(by_hand_block(result, config))
    plan = next(p for p in result.components if p.key == BANDAGE)
    assert plan.purchase_units == 9 and plan.order_units == 2000
    assert f"order {plan.purchase_units} × case of 240 — 2160 units, against 2000 units needed" in (
        section
    )
    assert "order 2000 units" not in section


def test_an_unconfirmed_pack_size_gives_no_quantity_at_all(config: LoadedConfig) -> None:
    """Where nobody has confirmed what a pack holds, a number in the box
    could mean either thing — so Shannon gives none and says why."""
    result = _result(config)
    section = "\n".join(by_hand_block(result, config))
    plan = next(p for p in result.components if p.key == GLOVES)
    assert plan.purchase_units is None
    assert f"{plan.order_units} units needed — pack size unconfirmed" in section


def test_a_line_with_no_provable_page_is_printed_unlinked(config: LoadedConfig) -> None:
    """Zach's own printing supplier has no catalogue to link to. That line
    still has to appear, saying so — an omitted line is an unbought one."""
    section = "\n".join(by_hand_block(_result(config), config))
    assert "no link: Shannon has no page she can prove is item CARD-REDBAG" in section


@pytest.mark.parametrize(
    "url",
    [
        "https://dynarex.com/products/x/33161-nasal-oxygen",
        "https://dynarex.com/products/x/43161-inoculation-loop",
        "https://dynarex.com/product_search/?q=3161",
        "http://dynarex.com/products/x/3161-krinkle-gauze",
    ],
)
def test_the_loader_keeps_no_url_that_does_not_name_its_own_part(url: str) -> None:
    """33161 and 43161 are different products, and a search page shows
    whichever it likes first. It warns rather than stopping the week: the
    arithmetic is still good, only the link cannot be trusted, so that one
    line prints bare and the warning says to fix it."""
    findings: list[Finding] = []
    assert _product_url(_entry(url), GAUZE, findings) is None
    assert [f.severity for f in findings] == [Severity.WARNING]
    assert not findings[0].blocks_run


def test_the_loader_keeps_the_url_of_the_part_itself() -> None:
    good = (
        "https://dynarex.com/products/disposable-medical-supplies/"
        "general-advanced-wound-care/3161-krinkle-gauze-roll--sterile"
    )
    findings: list[Finding] = []
    assert _product_url(_entry(good), GAUZE, findings) == good
    assert not findings


def test_the_live_dynarex_lines_all_carry_a_page_of_their_own(config: LoadedConfig) -> None:
    """Resolved once, by hand, through dynarex.com's own search; checked
    here so a later edit cannot quietly drop one back to unlinked."""
    for key, component in config.boms.components.items():
        if key.supplier != "dynarex":
            continue
        url = product_url(component)
        assert url is not None, key
        assert url.rsplit("/", 1)[-1].startswith(key.part), key


def test_only_a_real_asin_becomes_an_amazon_link() -> None:
    """A field holding something that is not an ASIN addresses no listing,
    and /dp/ of it is a link to whatever Amazon feels like showing."""
    assert amazon_product_url("B0FC56RYZ3") == "https://www.amazon.com/dp/B0FC56RYZ3"
    assert amazon_product_url("b0fc56ryz3") == "https://www.amazon.com/dp/B0FC56RYZ3"
    assert amazon_product_url(None) is None
    assert amazon_product_url("") is None
    assert amazon_product_url("3161") is None
    assert amazon_product_url("B0FC56RYZ3-2PACK") is None


def test_a_staged_line_is_never_described_as_something_to_go_and_buy(
    config: LoadedConfig,
) -> None:
    """And a by-hand line is never described as staged. The sections are
    built from the routing, so the two lists cannot overlap."""
    result = _result(config)
    staged = "\n".join(staged_block(result, config))
    hand = "\n".join(by_hand_block(result, config))
    for plan in result.components:
        if plan.order_units <= 0:
            continue
        if plan.routing.endswith("_cart"):
            assert plan.name in staged and plan.name not in hand
        elif plan.routing == "gap_list":
            assert plan.name in hand and plan.name not in staged


def _nar_result(config: LoadedConfig) -> ReplenishmentResult:
    """Enough tourniquet sales to put NAR lines on the cart route."""
    return ReplenishmentCalculator(
        config=config,
        stock={},
        velocity={"25-010": SalesVelocity(sku="25-010", units_sold=900, window_days=90)},
        inbound={},
        on_order={},
        today=TODAY,
        manual_proposals={},
    ).calculate()


def test_the_staged_section_does_not_say_these_are_in_a_cart_already(
    config: LoadedConfig,
) -> None:
    """Staging is a separate run from this email. Until it has run and
    said what it put in, the cart is empty — and the section says so
    rather than handing Zach a cart to go and order."""
    staged = "\n".join(staged_block(_nar_result(config), config))
    assert "nothing here is in a cart yet" in staged
    assert "already" not in staged


def test_staging_that_policy_refuses_is_not_promised(config: LoadedConfig) -> None:
    """`max_tier_this_phase: 0` refuses nar.stage_cart, so the run this
    section points at will not fill anything. Promising it leaves Zach
    waiting on a confirmation email that never arrives."""
    assert not _staging_is_authorised(config, "nar")
    staged = "\n".join(staged_block(_nar_result(config), config))
    assert "Policy refuses that staging run today" in staged


def test_an_authorised_supplier_is_not_flagged_as_refused(config: LoadedConfig) -> None:
    """Raise the ceiling to what nar.stage_cart costs and the caveat goes,
    so the line tracks policy rather than being printed unconditionally."""
    allowed = replace(config, policy=replace(config.policy, max_tier_this_phase=2))
    assert _staging_is_authorised(allowed, "nar")
    staged = "\n".join(staged_block(_nar_result(allowed), allowed))
    assert "Policy refuses" not in staged


def test_the_amazon_parts_list_is_all_linkable(config: LoadedConfig) -> None:
    """Every amazon_business part in the live file is an ASIN today. If one
    ever is not, this fails and the report prints that line unlinked."""
    for key, component in config.boms.components.items():
        if key.supplier != "amazon_business":
            continue
        assert product_url(component) == f"https://www.amazon.com/dp/{key.part}", key
    assert GLOVES in config.boms.components
