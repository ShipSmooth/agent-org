"""Staging the Dynarex cart is the same run, pointed at a different cart.

The parts that are supplier-specific are the ones proved here: the week's
dynarex_cart lines are the ones staged (and NAR's are left for NAR's own
run), the action the broker weighs is `dynarex.*` rather than NAR's, and a
live run handed the saved copy of the cart stops instead of rehearsing —
the bug that made four NAR lines look like narescue.com had refused them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from agent_org.config.models import LoadedConfig
from agent_org.db.connection import entity_session
from agent_org.integrations.carts import Cart, CartLine, CartRefusal, CartUnavailable
from agent_org.integrations.dynarex import DynarexFixtureCart, DynarexPortalCart
from agent_org.integrations.nar import NarCartClient, NarFixtureCart
from agent_org.runtime.staging import stage_supplier_cart, supplier_cart_for
from agent_org.runtime.worker import run_replenishment

DATA = Path(__file__).parent / "fixtures" / "golden" / "data"
MONDAY = datetime(2026, 3, 30, 6, 0, tzinfo=UTC)
WEEK = "2026-W14"


@dataclass
class RecordingCart:
    """The Dynarex cart as it stands before the run, and what went in."""

    supplier: str = "dynarex"
    added: list[tuple[str, int]] = field(default_factory=list)

    def read_cart(self) -> Cart:
        lines = [CartLine(sku="3553", name="Sterile Gauze Pad", quantity=2, price=Decimal("8.10"))]
        lines += [
            CartLine(sku=sku, name=sku, quantity=quantity, price=Decimal("12.34"))
            for sku, quantity in self.added
        ]
        return Cart(
            supplier=self.supplier,
            cart_id="/cart",
            lines=tuple(lines),
            grand_total=Decimal("16.20"),
        )

    def add_line(self, sku: str, quantity: int) -> CartLine:
        self.added.append((sku, quantity))
        return CartLine(sku=sku, name=sku, quantity=quantity)


def _week(conn: psycopg.Connection[tuple[object, ...]], config: LoadedConfig, output: Path) -> None:
    run_replenishment(
        conn=conn, config=config, fixtures=DATA, output_dir=output, now=MONDAY, again=False
    )


def test_a_dry_run_stages_the_weeks_dynarex_lines_and_nothing_of_nars(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    cart = RecordingCart()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        summary = stage_supplier_cart(
            conn=conn,
            config=golden_config,
            supplier="dynarex",
            output_dir=tmp_path,
            dry_run=True,
            week=WEEK,
            now=MONDAY,
            cart=cart,
        )

    assert summary.error is None, summary.error
    assert summary.staged > 0
    assert cart.added == [], "a dry run must not touch the portal's cart"
    staged = {line.sku for line in summary.plan.lines}
    assert staged and staged <= {"3161", "3553", "3173", "3683"}
    body = Path(summary.report_path or "").read_text(encoding="utf-8")
    assert "dynarex" in body.lower()
    assert "3553" in body, "what Zach already had in the cart is reported back to him"


def test_a_live_dynarex_run_handed_the_saved_cart_stops_rather_than_rehearsing(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        with pytest.raises(CartRefusal, match="saved copy of the cart"):
            stage_supplier_cart(
                conn=conn,
                config=golden_config,
                supplier="dynarex",
                output_dir=tmp_path,
                fixtures=DATA,
                dry_run=False,
                week=WEEK,
                now=MONDAY,
            )


def test_live_staging_is_refused_while_the_phase_ceiling_is_zero(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Tier 2, and the phase allows nothing above 0 — so nothing reaches
    dynarex.com until Zach names `dynarex.stage_cart` himself."""
    assert golden_config.policy.rules["dynarex.stage_cart"].tier == 2
    assert golden_config.policy.max_tier_this_phase == 0

    cart = RecordingCart()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        summary = stage_supplier_cart(
            conn=conn,
            config=golden_config,
            supplier="dynarex",
            output_dir=tmp_path,
            dry_run=False,
            week=WEEK,
            now=MONDAY,
            cart=cart,
        )

    assert cart.added == []
    assert summary.staged == 0
    assert summary.error is not None


def test_each_supplier_gets_its_own_cart_and_an_unknown_one_gets_none(
    golden_config: LoadedConfig,
) -> None:
    """Staging Dynarex against the NAR cart is the kind of mistake only a
    real cart would reveal, so the wrong name is refused by name."""
    saved = Path("tests/fixtures/golden/data")
    assert isinstance(supplier_cart_for("nar", saved, golden_config), NarFixtureCart)
    assert isinstance(supplier_cart_for("dynarex", saved, golden_config), DynarexFixtureCart)
    assert isinstance(supplier_cart_for("nar", None, golden_config), NarCartClient)
    assert isinstance(supplier_cart_for("dynarex", None, golden_config), DynarexPortalCart)

    with pytest.raises(CartUnavailable, match="no way to reach a 'amazon_business' cart"):
        supplier_cart_for("amazon_business", saved, golden_config)


def test_the_rehearsal_and_the_real_thing_are_different_actions_per_supplier(
    golden_config: LoadedConfig,
) -> None:
    """A phase exception naming NAR's live staging must not quietly open
    Dynarex's as well, which one shared action name would have done."""
    rules = golden_config.policy.rules
    assert rules["dynarex.plan_cart_staging"].tier == 0
    assert {"nar.stage_cart", "dynarex.stage_cart"} <= set(rules)

    only_nar = replace(golden_config.policy, phase_exceptions={"nar.stage_cart": 3})
    assert "dynarex.stage_cart" not in only_nar.phase_exceptions
