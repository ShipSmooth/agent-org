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

from agent_org.audit.log import AuditLog
from agent_org.config.models import LoadedConfig
from agent_org.db.connection import entity_session
from agent_org.integrations.carts import Cart, CartLine, CartRefusal, CartUnavailable
from agent_org.integrations.dynarex import DynarexFixtureCart, DynarexPortalCart
from agent_org.integrations.nar import NarCartClient, NarFixtureCart
from agent_org.runtime.staging import (
    SHANNON_CART_STAGING,
    stage_supplier_cart,
    staging_history,
    supplier_cart_for,
)
from agent_org.runtime.worker import run_replenishment
from agent_org.tasks.queue import TaskQueue

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


def test_a_dynarex_run_handed_nars_cart_refuses_before_it_reads_anything(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The shape of the bug Zach found: Dynarex's lines, NAR's cart.

    Before the per-supplier carts existed there was one cart for every
    supplier, so a Dynarex dry run planned Dynarex lines and then read and
    reported the NAR cart underneath them. A cart that answers to another
    supplier's name is now refused rather than described.
    """
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        with pytest.raises(CartRefusal, match="given the nar cart"):
            stage_supplier_cart(
                conn=conn,
                config=golden_config,
                supplier="dynarex",
                output_dir=tmp_path,
                dry_run=True,
                week=WEEK,
                now=MONDAY,
                cart=NarFixtureCart(fixture_dir=DATA),
            )


def test_the_report_names_the_cart_it_actually_read(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Reading the wrong cart was invisible on the page it was printed on."""
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        summary = stage_supplier_cart(
            conn=conn,
            config=golden_config,
            supplier="dynarex",
            output_dir=tmp_path,
            fixtures=DATA,
            dry_run=True,
            week=WEEK,
            now=MONDAY,
        )

    body = Path(summary.report_path or "").read_text(encoding="utf-8")
    assert "read from the dynarex cart" in body
    assert "30-0002" not in body, "NAR's cart has no business in a Dynarex report"


def test_nars_live_weeks_are_no_yardstick_for_dynarexs_first(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
) -> None:
    """ "Is this week unusual?" is asked of the supplier's own past weeks.

    A first live run at a supplier escalates to Tier 3 because there is
    nothing to call it unusual against, and weeks staged at NAR say
    nothing about what a normal Dynarex week looks like.
    """
    with entity_session(app_conn, entity_id) as conn:
        audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
        queue = TaskQueue(conn=conn, entity_id=entity_id, audit=audit)
        for week in ("2026-W10", "2026-W11", "2026-W12", "2026-W13", "2026-W14"):
            slot = f"{SHANNON_CART_STAGING}/nar/{week}"
            queue.enqueue(SHANNON_CART_STAGING, slot)
            task = queue.claim((SHANNON_CART_STAGING,), schedule_slot=slot)
            assert task is not None
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cart_stagings (entity_id, task_id, supplier, schedule_slot,
                                               sku, quantity, units, mode, status)
                    VALUES (%s, %s, 'nar', %s, '80-0167', 20, 20, 'LIVE', 'ADDED')
                    """,
                    (entity_id, task.id, slot),
                )

        assert staging_history(conn, entity_id, "nar").order_count == 5
        assert staging_history(conn, entity_id, "dynarex").order_count == 0


def _with_exception(config: LoadedConfig, up_to_tier: int) -> LoadedConfig:
    policy = replace(config.policy, phase_exceptions={"dynarex.stage_cart": up_to_tier})
    return replace(config, policy=policy)


def test_a_first_live_dynarex_week_needs_the_exception_to_reach_tier_3(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """`up_to_tier: 2` is not enough for a supplier's first live week.

    Staging is weighed as a purchase, and with no past live weeks at this
    supplier the anomaly rules escalate to Tier 3 — there is nothing to
    call the week unusual against. Zach saying 3 is Zach saying he knows
    that and is deciding anyway.
    """
    cart = RecordingCart()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        refused = stage_supplier_cart(
            conn=conn,
            config=_with_exception(golden_config, 2),
            supplier="dynarex",
            output_dir=tmp_path,
            dry_run=False,
            week=WEEK,
            now=MONDAY,
            cart=cart,
        )
    assert cart.added == []
    assert refused.error is not None
    assert "nothing can be called normal yet" in refused.error

    with entity_session(app_conn, entity_id) as conn:
        allowed = stage_supplier_cart(
            conn=conn,
            config=_with_exception(golden_config, 3),
            supplier="dynarex",
            output_dir=tmp_path,
            dry_run=False,
            week=WEEK,
            now=MONDAY,
            cart=cart,
            again=True,
        )
    assert allowed.error is None, allowed.error
    assert cart.added, "with Tier 3 allowed, the week's lines reach the portal's cart"
