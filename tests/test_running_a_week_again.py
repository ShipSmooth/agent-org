"""Re-running a week, and what a re-run is not allowed to repeat.

One guard used to cover two very different things: regenerating a report,
which reads and writes a file, and repeating an action with an effect
outside this machine, which is the failure this system exists to prevent.
The safe case was blocked by a rule written for the dangerous one.

`--again` separates them. These tests hold both halves: the report can be
produced again as often as Zach likes, and the fingerprint protecting a
Tier 1 action is untouched by the flag that does it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_org.audit.log import AuditLog
from agent_org.broker.broker import ActionBroker
from agent_org.broker.proposals import ProposalStatus
from agent_org.broker.registry import Executor, ExecutorRegistry
from agent_org.config.models import LoadedConfig
from agent_org.db.connection import entity_session
from agent_org.policy.engine import PolicyEngine
from agent_org.runtime.worker import RunAlreadyDone, RunSummary, run_replenishment

DATA = Path(__file__).parent / "fixtures" / "golden" / "data"
WHEN = datetime(2026, 3, 30, 6, 0, tzinfo=UTC)
LATER = datetime(2026, 3, 30, 9, 30, tzinfo=UTC)


def _run(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    output_dir: Path,
    when: datetime,
    again: bool = False,
) -> RunSummary:
    return run_replenishment(
        conn=conn,
        config=config,
        fixtures=DATA,
        output_dir=output_dir,
        now=when,
        again=again,
    )


def test_without_the_flag_a_second_run_is_still_refused(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The guard is kept, and now says how to do the harmless thing."""
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, WHEN)
        with pytest.raises(RunAlreadyDone) as caught:
            _run(conn, golden_config, tmp_path, LATER)
    message = str(caught.value)
    assert "already been carried out" in message
    assert "shannon run --again" in message


def test_again_writes_a_second_report_and_records_which_it_replaced(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Both reports stay in the database, and the old one is marked."""
    with entity_session(app_conn, entity_id) as conn:
        first = _run(conn, golden_config, tmp_path, WHEN)
        second = _run(conn, golden_config, tmp_path, LATER, again=True)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, superseded_by, superseded_at IS NOT NULL
                  FROM reports WHERE entity_id = %s ORDER BY created_at
                """,
                (entity_id,),
            )
            rows = cur.fetchall()

    assert len(rows) == 2
    (old_id, superseded_by, marked), (new_id, still_current, unmarked) = rows
    assert str(superseded_by) == str(new_id)
    assert marked is True
    assert still_current is None
    assert unmarked is False

    # And the run says so, rather than leaving it in the table.
    assert second.superseded_report_id == str(old_id)
    assert second.superseded_written_at is not None
    assert first.outcome is not None and second.outcome is not None
    assert first.outcome.broker_outcome is not None
    assert second.outcome.broker_outcome is not None
    assert second.outcome.broker_outcome.duplicate_of is None


def test_the_replaced_report_file_is_kept_beside_the_new_one(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Zach reads the folder, not the table. Overwriting the file would
    leave the database honest and the folder misleading."""
    with entity_session(app_conn, entity_id) as conn:
        first = _run(conn, golden_config, tmp_path, WHEN)
        second = _run(conn, golden_config, tmp_path, LATER, again=True)

    assert first.report_path == second.report_path
    kept = second.superseded_path
    assert kept is not None
    assert Path(kept).exists()
    assert "superseded-" in Path(kept).name
    assert Path(str(second.report_path)).exists()


def test_again_re_reads_and_re_reports_and_nothing_else(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Every proposal filed by both runs is the Tier 0 report, executed.

    Nothing was staged, nothing was sent, and there is no proposal of any
    other kind for a re-run to have repeated.
    """
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, WHEN)
        _run(conn, golden_config, tmp_path, LATER, again=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action_type, tier, status FROM action_proposals WHERE entity_id = %s",
                (entity_id,),
            )
            proposals = cur.fetchall()
    assert len(proposals) == 2
    for action_type, tier, status in proposals:
        assert action_type == "internal.write_draft_report"
        assert tier == 0
        assert status == ProposalStatus.EXECUTED.value


def test_again_on_a_week_that_was_never_run_just_runs_it(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        summary = _run(conn, golden_config, tmp_path, WHEN, again=True)
    assert summary.error is None
    assert summary.superseded_report_id is None


def test_an_action_with_an_outside_effect_keeps_its_fingerprint_on_a_rerun(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
) -> None:
    """The rule that matters in Phase 2, proved in Phase 1.

    A re-run marks its attempt with a salt so a report can be written
    again. If that salt reached staging a cart, `--again` would stage the
    cart twice — so the broker drops it above Tier 0, and the second submit
    is recognised as the same business action and not carried out again.

    The policy here is raised to allow the action deliberately. Phase 1
    cannot reach it at all, which is precisely why the rule is worth
    proving now: by the phase that can, the mistake costs money.
    """
    calls: list[dict[str, Any]] = []

    def stage_cart(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"cart": len(calls)}

    registry = ExecutorRegistry()
    registry.register(
        Executor(
            action_type="supplier.stage_cart",
            reversible="window",
            category="purchase",
            supplier=None,
            requires_capability=None,
            run=stage_cart,
        )
    )
    with entity_session(app_conn, entity_id) as conn:
        audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
        policy = PolicyEngine(replace(golden_config.policy, max_tier_this_phase=3))
        broker = ActionBroker(
            conn=conn,
            entity_id=entity_id,
            policy=policy,
            registry=registry,
            audit=audit,
            suppliers=golden_config.boms.suppliers,
        )
        payload = {"supplier": "nar", "lines": [{"sku": "30-0001", "units": 600}]}
        task_id = _a_task(conn, entity_id)
        first = broker.submit(
            action_type="supplier.stage_cart",
            payload=payload,
            task_id=task_id,
            schedule_slot="shannon_replenishment/2026-W14",
        )
        second = broker.submit(
            action_type="supplier.stage_cart",
            payload=payload,
            task_id=task_id,
            schedule_slot="shannon_replenishment/2026-W14",
            attempt_salt="attempt-2",
        )

    assert first.tier >= 1
    assert second.duplicate_of == first.proposal_id
    assert len(calls) == 1, "the cart was staged twice by a re-run"


def _a_task(conn: psycopg.Connection[tuple[object, ...]], entity_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (entity_id, kind, schedule_slot)
            VALUES (%s, 'shannon_replenishment', 'shannon_replenishment/2026-W14')
            RETURNING id
            """,
            (entity_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return str(row[0])
