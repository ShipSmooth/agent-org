"""Staging the NAR cart end to end, against a real database and a fake cart.

Three things are being proved, and only the first is about carts:

1. A dry run reads the supplier's cart and changes nothing in it.
2. Running it twice does not stage the same SKU twice, because a cart line
   cannot be taken back out once added.
3. Live staging is refused while the phase ceiling is 0, and refused by the
   broker rather than by the caller remembering to ask.

No credential exists here and no request leaves the machine: the cart is a
recorder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from agent_org.audit.log import AuditLog
from agent_org.broker.executors.supplier_cart import ACTION_STAGE_CART, CartStager
from agent_org.config.models import Capability, LoadedConfig
from agent_org.db.connection import entity_session
from agent_org.integrations.carts import Cart, CartLine, CartRefusal, CartUnavailable
from agent_org.notify.email import RecordingSender
from agent_org.runtime.staging import (
    SHANNON_CART_STAGING,
    NothingToStage,
    StagingSummary,
    deliver_staging_report,
    stage_supplier_cart,
)
from agent_org.runtime.worker import run_replenishment
from agent_org.shannon.staging import StagingPlan
from agent_org.shannon.staging_report import (
    NOTHING_WAS_SUBMITTED,
    StagingContext,
    render,
)
from agent_org.tasks.queue import TaskQueue

DATA = Path(__file__).parent / "fixtures" / "golden" / "data"
MONDAY = datetime(2026, 3, 30, 6, 0, tzinfo=UTC)
WEEK = "2026-W14"


@dataclass
class RecordingCart:
    """A cart that remembers, and a checkout that does not exist on it."""

    supplier: str = "nar"
    added: list[tuple[str, int]] = field(default_factory=list)
    fail_on: str | None = None

    def read_cart(self) -> Cart:
        lines = [CartLine(sku="30-0002", name="C-A-T", quantity=4, price=Decimal("27.99"))]
        lines += [
            CartLine(sku=sku, name=sku, quantity=qty, price=Decimal("1.00"))
            for sku, qty in self.added
        ]
        return Cart(
            supplier=self.supplier,
            cart_id="quote-1",
            lines=tuple(lines),
            grand_total=Decimal("111.96"),
        )

    def add_line(self, sku: str, quantity: int) -> CartLine:
        if sku == self.fail_on:
            raise CartUnavailable(f"narescue.com answered 500 for {sku}.")
        self.added.append((sku, quantity))
        return CartLine(sku=sku, name=sku, quantity=quantity)


def _week(conn: psycopg.Connection[tuple[object, ...]], config: LoadedConfig, output: Path) -> None:
    run_replenishment(
        conn=conn, config=config, fixtures=DATA, output_dir=output, now=MONDAY, again=False
    )


def _stage(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    output: Path,
    cart: RecordingCart,
    dry_run: bool = True,
) -> StagingSummary:
    return stage_supplier_cart(
        conn=conn,
        config=config,
        supplier="nar",
        output_dir=output,
        dry_run=dry_run,
        week=WEEK,
        now=MONDAY,
        cart=cart,
    )


def _says(body: str, sentence: str) -> bool:
    """The report wraps its lines, so compare on words rather than layout."""
    return " ".join(sentence.split()) in " ".join(body.split())


def _staged_rows(
    conn: psycopg.Connection[tuple[object, ...]], entity_id: str
) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sku, status FROM cart_stagings WHERE entity_id = %s ORDER BY sku", (entity_id,)
        )
        return [(str(row[0]), str(row[1])) for row in cur.fetchall()]


def _a_task(conn: psycopg.Connection[tuple[object, ...]], entity_id: str) -> str:
    """A task row for the ledger's foreign key to point at."""
    audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
    queue = TaskQueue(conn=conn, entity_id=entity_id, audit=audit)
    slot = "shannon_cart_staging/nar/2026-W14"
    queue.enqueue(SHANNON_CART_STAGING, slot)
    task = queue.claim((SHANNON_CART_STAGING,), schedule_slot=slot)
    assert task is not None
    return task.id


def test_a_dry_run_reads_the_cart_and_adds_nothing_to_it(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    cart = RecordingCart()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        summary = _stage(conn, golden_config, tmp_path, cart)
    assert summary.error is None, summary.error
    assert cart.added == [], "a dry run must not touch the supplier's cart"
    assert summary.staged > 0
    body = Path(summary.report_path or "").read_text(encoding="utf-8")
    assert "DRY RUN" in body
    assert _says(body, NOTHING_WAS_SUBMITTED)


def test_what_zach_already_put_in_the_cart_is_reported_and_left_alone(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    cart = RecordingCart()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        summary = _stage(conn, golden_config, tmp_path, cart)
    body = Path(summary.report_path or "").read_text(encoding="utf-8")
    assert "THE CART BEFORE THIS RUN" in body
    assert "30-0002" in body
    assert "left exactly as it was" in body


def test_running_the_same_week_again_does_not_stage_it_again(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The broker recognises the second run as the same action and hands
    back the first one's answer rather than doing it again."""
    cart = RecordingCart()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        first = _stage(conn, golden_config, tmp_path, cart)
        _stage(conn, golden_config, tmp_path, cart)
        rows = _staged_rows(conn, entity_id)
    assert first.staged > 0
    assert cart.added == []
    # One ledger row per SKU, not two: the second run added nothing to it.
    assert len(rows) == len({sku for sku, _ in rows}) == first.staged


def test_the_ledger_stops_a_line_being_added_to_the_cart_twice(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The executor itself, below the broker, refuses to add a SKU this week
    has already put in the cart. A cart line cannot be taken back out, so
    this is the guard that has to hold when a run crashes and is retried
    with a payload the broker sees as new."""
    cart = RecordingCart()
    payload = {
        "schedule_slot": "shannon_cart_staging/nar/2026-W14",
        "lines": [{"sku": "80-0167", "name": "IPOK", "quantity": 20, "units": 20}],
    }
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        task_id = _a_task(conn, entity_id)
        stager = CartStager(
            conn=conn, entity_id=entity_id, supplier="nar", cart=cart, dry_run=False
        )
        first = stager({**payload, "task_id": task_id})
        second = stager({**payload, "task_id": task_id})
    assert first["lines"][0]["status"] == "ADDED"
    assert second["lines"][0]["status"] == "SKIPPED"
    assert cart.added == [("80-0167", 20)], "the SKU must reach the cart exactly once"


def test_a_line_the_site_refuses_is_reported_and_the_rest_still_go_in(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    cart = RecordingCart(fail_on="80-0167")
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        task_id = _a_task(conn, entity_id)
        stager = CartStager(
            conn=conn, entity_id=entity_id, supplier="nar", cart=cart, dry_run=False
        )
        result = stager(
            {
                "task_id": task_id,
                "schedule_slot": "shannon_cart_staging/nar/2026-W14",
                "lines": [
                    {"sku": "80-0167", "name": "IPOK", "quantity": 20, "units": 20},
                    {"sku": "30-0002", "name": "C-A-T", "quantity": 400, "units": 400},
                ],
            }
        )
    statuses = {line["sku"]: line["status"] for line in result["lines"]}
    assert statuses == {"80-0167": "FAILED", "30-0002": "ADDED"}
    assert cart.added == [("30-0002", 400)]
    assert result["submitted"] is False


@dataclass
class ForgetfulCart(RecordingCart):
    """A cart that says yes and then does not hold the line."""

    def read_cart(self) -> Cart:
        return Cart(
            supplier=self.supplier,
            cart_id="quote-1",
            lines=(CartLine(sku="30-0002", name="C-A-T", quantity=4, price=Decimal("27.99")),),
            grand_total=Decimal("111.96"),
        )


@dataclass
class ForgetfulOfZachsCart(RecordingCart):
    """A cart that loses what was in it before the run."""

    read_count: int = 0

    def read_cart(self) -> Cart:
        self.read_count += 1
        lines = [CartLine(sku=sku, name=sku, quantity=qty) for sku, qty in self.added]
        if self.read_count == 1:
            lines.insert(0, CartLine(sku="30-0002", name="C-A-T", quantity=4))
        return Cart(supplier=self.supplier, cart_id="quote-1", lines=tuple(lines))


def _live_stage(
    conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    cart: RecordingCart,
) -> dict[str, object]:
    task_id = _a_task(conn, entity_id)
    stager = CartStager(conn=conn, entity_id=entity_id, supplier="nar", cart=cart, dry_run=False)
    return stager(
        {
            "task_id": task_id,
            "schedule_slot": "shannon_cart_staging/nar/2026-W14",
            "lines": [{"sku": "80-0167", "name": "IPOK", "quantity": 20, "units": 20}],
        }
    )


def test_a_line_the_cart_does_not_actually_hold_afterwards_is_not_called_added(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """A 200 from the site is the site's account of the cart, not the cart."""
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        result = _live_stage(conn, entity_id, ForgetfulCart())
    line = result["lines"][0]  # type: ignore[index]
    assert line["verified"] is False
    assert "Check the cart on the site" in line["detail"]


def test_a_verified_line_says_so(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        result = _live_stage(conn, entity_id, RecordingCart())
    assert result["lines"][0]["verified"] is True  # type: ignore[index]
    assert result["kept"] == {"all_kept": True, "lost": []}


def test_a_line_of_zachs_that_vanishes_during_the_run_is_reported(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Shannon cannot remove a line, so if one goes, Zach has to be told."""
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        result = _live_stage(conn, entity_id, ForgetfulOfZachsCart())
    assert result["kept"] == {
        "all_kept": False,
        "lost": [{"sku": "30-0002", "was": 4, "now": 0}],
    }


def test_live_staging_is_refused_while_the_phase_is_read_only(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """max_tier_this_phase is 0, so the broker stops it before the cart is
    ever asked to add anything."""
    cart = RecordingCart()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        summary = _stage(conn, golden_config, tmp_path, cart, dry_run=False)
    assert summary.error is not None
    assert "read-only" in summary.error or "tier" in summary.error
    assert cart.added == []


def test_staging_a_week_that_was_never_reported_is_refused(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    with (
        entity_session(app_conn, entity_id) as conn,
        pytest.raises(NothingToStage, match="no replenishment report"),
    ):
        _stage(conn, golden_config, tmp_path, RecordingCart())


def test_the_confirmation_email_goes_to_the_owner_and_says_nothing_was_submitted(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        summary = _stage(conn, golden_config, tmp_path, RecordingCart())
        summary = deliver_staging_report(
            conn=conn, config=golden_config, summary=summary, sender=sender
        )
    assert summary.email_error is None, summary.email_error
    assert len(sender.sent) == 1
    mail = sender.sent[0]
    assert mail.to == ("zach@ithrivemedical.com",)
    assert "dry run" in mail.subject
    assert _says(mail.body, NOTHING_WAS_SUBMITTED)


def test_the_report_tells_zach_to_look_when_a_line_cannot_be_verified() -> None:
    """The confirmation is worthless if it says 'added' about a line that is
    not in the cart, so the doubt is the first thing on the page."""
    plan = StagingPlan(supplier="nar", lines=(), skipped=())
    result = {
        "mode": "LIVE",
        "lines": [
            {
                "sku": "80-0167",
                "name": "IPOK",
                "quantity": 20,
                "status": "ADDED",
                "verified": False,
                "detail": "the cart afterwards holds 0 of it",
            }
        ],
        "kept": {"all_kept": False, "lost": [{"sku": "30-0002", "was": 4, "now": 1}]},
        "cart_before": {},
        "cart_after": {},
    }
    body = render(
        plan,
        result,
        StagingContext(
            supplier_name="narescue.com",
            entity_name="iThrive Medical LLC",
            week=WEEK,
            generated_at=MONDAY,
        ),
    )
    assert _says(body, "CHECK THE CART YOURSELF: 1 line went in, but the cart afterwards")
    assert _says(body, "the cart held 1 line before this run that it no longer holds in full")


def test_nothing_anywhere_registers_a_way_to_buy(golden_config: LoadedConfig) -> None:
    """No supplier holds `purchase`, so the live action cannot run even if a
    future phase raises the ceiling for something else."""
    assert all(
        not supplier.can(Capability.PURCHASE) for supplier in golden_config.boms.suppliers.values()
    )
    assert ACTION_STAGE_CART in golden_config.policy.rules, (
        "live staging must be a named, tiered action rather than an unlisted default"
    )


def test_a_live_run_handed_a_saved_cart_refuses_rather_than_rehearsing(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """A saved cart refusing a line must never read like narescue.com did."""
    with entity_session(app_conn, entity_id) as conn:
        _week(conn, golden_config, tmp_path)
        with pytest.raises(CartRefusal, match="saved copy of the cart"):
            stage_supplier_cart(
                conn=conn,
                config=golden_config,
                supplier="nar",
                output_dir=tmp_path,
                fixtures=DATA,
                dry_run=False,
                week=WEEK,
                now=MONDAY,
            )
        assert _staged_rows(conn, entity_id) == []
