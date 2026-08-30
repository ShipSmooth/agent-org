"""The doorway: default-deny policy, the Phase 1 ceiling, and fingerprints."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_org.audit.log import AuditLog
from agent_org.broker.broker import ActionBroker, BrokerRefusal
from agent_org.broker.executors.internal_report import ReportWriter, build_registry
from agent_org.broker.proposals import ProposalStatus, fingerprint
from agent_org.broker.registry import Executor
from agent_org.config.errors import ConfigError
from agent_org.config.loader import load_policy
from agent_org.config.models import Capability, LoadedConfig
from agent_org.db.connection import entity_session
from agent_org.policy.engine import ActionContext, PolicyEngine, TrailingHistory
from agent_org.tasks.queue import TaskQueue, schedule_slot

SLOT = "shannon_replenishment/2026-W20"


def _broker(
    conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    config: LoadedConfig,
    tmp_path: Path,
) -> tuple[ActionBroker, Any]:
    audit = AuditLog(conn=conn, entity_id=entity_id, actor="shannon")
    registry = build_registry(ReportWriter(conn=conn, entity_id=entity_id, output_dir=tmp_path))
    broker = ActionBroker(
        conn=conn,
        entity_id=entity_id,
        policy=PolicyEngine(config.policy),
        registry=registry,
        audit=audit,
        suppliers=config.boms.suppliers,
    )
    return broker, registry


def _task_id(conn: psycopg.Connection[tuple[object, ...]], entity_id: str) -> str:
    queue = TaskQueue(conn, entity_id, AuditLog(conn=conn, entity_id=entity_id, actor="test"))
    queue.enqueue("shannon_replenishment", SLOT)
    task = queue.claim(("shannon_replenishment",))
    assert task is not None
    return task.id


def _report_payload(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "filename": "report.md",
        "body": "a report",
        "bom_version": "test-1",
        "config_digest": "abc123",
        "parameters": {"cover_target_weeks": 7},
        "lines": [],
    }


def test_an_unknown_action_is_denied_by_default(golden_config: LoadedConfig) -> None:
    decision = PolicyEngine(golden_config.policy).resolve("something.nobody.wrote")
    assert decision.tier == 3
    assert not decision.matched_a_rule


def test_an_irreversible_action_is_tier_3_whatever_the_rule_says(
    golden_config: LoadedConfig,
) -> None:
    decision = PolicyEngine(golden_config.policy).resolve(
        "veeqo.read_inventory", ActionContext(reversible="no")
    )
    assert decision.tier == 3
    assert "cannot be undone" in " ".join(decision.reasons)


def test_a_purchase_with_too_little_history_is_tier_3(golden_config: LoadedConfig) -> None:
    decision = PolicyEngine(golden_config.policy).resolve(
        "nar.stage_cart",
        ActionContext(category="purchase", total_usd=Decimal("500")),
        TrailingHistory(order_count=1),
    )
    assert decision.tier == 3
    assert "nothing can be called normal yet" in " ".join(decision.reasons)


def test_an_entity_can_raise_a_global_tier_but_never_lower_one(tmp_path: Path) -> None:
    """A business's own file may be stricter than the house rules and never
    laxer, or the ceiling could be edited away one entity at a time."""
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "global.yaml").write_text(
        "default_tier: 3\nmax_tier_this_phase: 0\n"
        "rules:\n  - {action: nar.stage_cart, tier: 2}\n"
        "  - {action: internal.write_draft_report, tier: 0}\n",
        encoding="utf-8",
    )
    global_policy = tmp_path / "policy" / "global.yaml"
    stricter = tmp_path / "stricter.yaml"
    stricter.write_text(
        "rules:\n  - {action: internal.write_draft_report, tier: 2}\n", encoding="utf-8"
    )
    engine = PolicyEngine(load_policy(global_policy, stricter))
    assert engine.resolve("internal.write_draft_report").tier == 2
    assert engine.resolve("nar.stage_cart").tier == 2

    laxer = tmp_path / "laxer.yaml"
    laxer.write_text("rules:\n  - {action: nar.stage_cart, tier: 0}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_policy(global_policy, laxer)
    assert "may raise a tier, never lower one" in caught.value.findings[0].message


def _policy_file(tmp_path: Path, exceptions: str) -> Path:
    path = tmp_path / "global.yaml"
    path.write_text(
        "default_tier: 3\nmax_tier_this_phase: 0\n"
        "rules:\n  - {action: nar.stage_cart, tier: 2}\n"
        "  - {action: dynarex.stage_cart, tier: 2}\n"
        f"phase_exceptions:{exceptions}\n",
        encoding="utf-8",
    )
    return path


def test_one_action_can_be_let_above_the_ceiling_without_letting_the_rest(
    tmp_path: Path,
) -> None:
    """How live NAR staging gets switched on: by name, not by raising the phase."""
    exception = "\n  - {action: nar.stage_cart, up_to_tier: 2}"
    policy = load_policy(_policy_file(tmp_path, exception), None)
    engine = PolicyEngine(policy)
    assert engine.ceiling_for("nar.stage_cart") == 2
    assert engine.ceiling_for("dynarex.stage_cart") == 0
    assert policy.max_tier_this_phase == 0


def test_with_no_exception_written_down_nothing_is_above_the_ceiling(tmp_path: Path) -> None:
    engine = PolicyEngine(load_policy(_policy_file(tmp_path, " []"), None))
    assert engine.ceiling_for("nar.stage_cart") == 0


def test_an_exception_has_to_say_how_far_it_goes(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_policy(_policy_file(tmp_path, "\n  - {action: nar.stage_cart}"), None)
    assert "no 'up_to_tier'" in caught.value.findings[0].message


def test_an_exception_cannot_be_the_only_mention_of_an_action(tmp_path: Path) -> None:
    """Otherwise a typo invents an action and permits it in one line."""
    with pytest.raises(ConfigError) as caught:
        load_policy(_policy_file(tmp_path, "\n  - {action: nar.stage_carts, up_to_tier: 2}"), None)
    assert "has no rule" in caught.value.findings[0].message


def test_only_the_report_writer_is_wired_up_in_this_phase(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    tmp_path: Path,
) -> None:
    """Nothing that sends, buys or browses is registered at all, so it
    could not run even if the policy said it might."""
    with entity_session(app_conn, entity_id) as conn:
        registry = build_registry(ReportWriter(conn=conn, entity_id=entity_id, output_dir=tmp_path))
    assert sorted(registry.action_types()) == ["internal.write_draft_report"]


def test_phase_1_refuses_anything_above_tier_0(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The point of the phase. A Tier 1 action is stopped at the doorway."""
    with entity_session(app_conn, entity_id) as conn:
        broker, registry = _broker(conn, entity_id, golden_config, tmp_path)
        registry.register(
            Executor(
                action_type="internal.enqueue_task",  # tier 1 in the rulebook
                reversible="yes",
                category="internal",
                supplier=None,
                requires_capability=None,
                run=lambda payload: {"did": "something"},
            )
        )
        task_id = _task_id(conn, entity_id)
        with pytest.raises(BrokerRefusal) as caught:
            broker.submit("internal.enqueue_task", {}, task_id=task_id, schedule_slot=SLOT)
        assert caught.value.tier == 1
        assert "read-only" in caught.value.reason

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM action_proposals WHERE entity_id = %s", (entity_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 0, "a refused action must not leave a proposal behind"


def test_an_action_with_no_executor_is_refused(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        broker, _ = _broker(conn, entity_id, golden_config, tmp_path)
        task_id = _task_id(conn, entity_id)
        with pytest.raises(BrokerRefusal) as caught:
            broker.submit("nar.stage_cart", {}, task_id=task_id, schedule_slot=SLOT)
        assert "no executor" in caught.value.reason


def test_capability_is_checked_before_policy(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """A supplier that cannot be carted is refused on capability, not tier."""
    with entity_session(app_conn, entity_id) as conn:
        broker, registry = _broker(conn, entity_id, golden_config, tmp_path)
        registry.register(
            Executor(
                action_type="nar.stage_cart",
                reversible="yes",
                category="purchase",
                supplier="nar",
                requires_capability=Capability.PURCHASE,
                run=lambda payload: {},
            )
        )
        task_id = _task_id(conn, entity_id)
        with pytest.raises(BrokerRefusal) as caught:
            broker.submit("nar.stage_cart", {}, task_id=task_id, schedule_slot=SLOT)
        assert caught.value.tier is None
        assert "not set up for" in caught.value.reason


def test_a_tier_0_report_is_executed_and_recorded(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        broker, _ = _broker(conn, entity_id, golden_config, tmp_path)
        task_id = _task_id(conn, entity_id)
        outcome = broker.submit(
            "internal.write_draft_report",
            _report_payload(task_id),
            task_id=task_id,
            schedule_slot=SLOT,
        )
        assert outcome.tier == 0
        assert outcome.status is ProposalStatus.EXECUTED
        assert Path(str(outcome.result["file_path"])).read_text(encoding="utf-8") == "a report"


def test_the_same_run_twice_does_not_write_two_reports(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    with entity_session(app_conn, entity_id) as conn:
        broker, _ = _broker(conn, entity_id, golden_config, tmp_path)
        task_id = _task_id(conn, entity_id)
        payload = _report_payload(task_id)
        first = broker.submit(
            "internal.write_draft_report", payload, task_id=task_id, schedule_slot=SLOT
        )
        second = broker.submit(
            "internal.write_draft_report", payload, task_id=task_id, schedule_slot=SLOT
        )
        assert second.duplicate_of == first.proposal_id

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM reports WHERE entity_id = %s", (entity_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_a_deliberate_rerun_is_a_different_fingerprint() -> None:
    payload = {"filename": "report.md", "body": "a report"}
    key = fingerprint("ithrive", "internal.write_draft_report", payload, SLOT)
    same = fingerprint("ithrive", "internal.write_draft_report", dict(payload), SLOT)
    rerun = fingerprint("ithrive", "internal.write_draft_report", payload, SLOT, "rerun-1")
    other_entity = fingerprint("limazulu", "internal.write_draft_report", payload, SLOT)
    reordered = fingerprint(
        "ithrive",
        "internal.write_draft_report",
        {"body": "a report", "filename": "report.md"},
        SLOT,
    )
    assert key == same
    assert key == reordered, "the order keys happen to be written in must not matter"
    assert key != rerun
    assert key != other_entity


def test_a_failing_executor_leaves_a_failed_proposal(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The log never claims less than what happened, including the mess."""

    def explode(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("the disk is full")

    with entity_session(app_conn, entity_id) as conn:
        broker, registry = _broker(conn, entity_id, golden_config, tmp_path)
        registry.register(
            Executor(
                action_type="internal.write_draft_report",
                reversible="yes",
                category="internal",
                supplier=None,
                requires_capability=None,
                run=explode,
            )
        )
        task_id = _task_id(conn, entity_id)
        with pytest.raises(RuntimeError):
            broker.submit(
                "internal.write_draft_report",
                _report_payload(task_id),
                task_id=task_id,
                schedule_slot=schedule_slot("shannon_replenishment"),
            )
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM action_proposals WHERE entity_id = %s", (entity_id,))
            rows = [str(row[0]) for row in cur.fetchall()]
        assert rows == ["FAILED"]
