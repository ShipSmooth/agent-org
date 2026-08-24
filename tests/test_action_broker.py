"""ActionBroker — the single doorway. Phase 1 lets nothing above Tier 0 out."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from agent_org.broker.actions import ActionRegistry, ActionSpec
from agent_org.broker.broker import ActionBroker, BrokerRefusalError
from agent_org.policy.engine import PolicyEngine
from agent_org.runtime.tasks import enqueue
from agent_org.runtime.wiring import build_broker
from agent_org.shannon.config_model import EntityConfig
from agent_org.tenancy.session import entity_session

CONFIG = Path(__file__).resolve().parents[1] / "config"
SLOT = "2026-08-24"


@pytest.fixture
def broker(golden_cfg: EntityConfig) -> ActionBroker:
    return build_broker(golden_cfg, CONFIG)


@pytest.fixture
def task_id(clean_db: psycopg.Connection) -> str:
    tid = enqueue(clean_db, "ithrive", "shannon.replenishment", SLOT)
    assert tid is not None
    clean_db.commit()
    return tid


def _proposal(conn: psycopg.Connection, proposal_id: str) -> tuple[str, int, dict[str, object]]:
    with entity_session(conn, "ithrive"):
        row = conn.execute(
            "SELECT status, tier, result FROM action_proposals WHERE id = %s", (proposal_id,)
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), row[2] if isinstance(row[2], dict) else {}


def test_a_tier_zero_report_write_executes(
    clean_db: psycopg.Connection, broker: ActionBroker, task_id: str, tmp_path: Path
) -> None:
    outcome = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="internal.write_draft_report",
        payload={
            "content": "# A report\n",
            "out_dir": str(tmp_path),
            "kind": "shannon.replenishment",
            "schedule_slot": SLOT,
            "bom_version": "test",
            "config_snapshot": json.dumps({}),
            "task_id": task_id,
        },
        schedule_slot=SLOT,
    )
    assert outcome.status == "EXECUTED"
    assert outcome.tier == 0
    assert _proposal(clean_db, outcome.proposal_id)[0] == "EXECUTED"


def test_a_tier_one_action_is_refused_in_this_phase(
    clean_db: psycopg.Connection, broker: ActionBroker, task_id: str
) -> None:
    outcome = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="internal.state_write",
        payload={"what": "anything at all"},
        schedule_slot=SLOT,
    )
    assert outcome.status == "REJECTED"
    assert outcome.tier >= 1
    status, _, result = _proposal(clean_db, outcome.proposal_id)
    assert status == "REJECTED"
    assert "Phase 1" in str(result["reason"])


def test_a_tier_two_purchase_action_is_refused_in_this_phase(
    clean_db: psycopg.Connection, broker: ActionBroker, task_id: str
) -> None:
    outcome = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="nar.stage_cart",
        payload={"supplier": "nar", "lines": [{"sku": "30-0001", "qty": 600}]},
        schedule_slot=SLOT,
    )
    # Tier 2 by rule, escalated because no executor is wired in this phase and
    # an unwired action's reversibility is unknown — either way, refused.
    assert outcome.status == "REJECTED"
    assert outcome.tier >= 2


def test_an_unknown_action_is_refused_by_default(
    clean_db: psycopg.Connection, broker: ActionBroker, task_id: str
) -> None:
    outcome = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="nar.checkout_and_pay",
        payload={},
        schedule_slot=SLOT,
    )
    assert (outcome.status, outcome.tier) == ("REJECTED", 3)


def test_a_refusal_is_still_recorded_before_it_is_refused(
    clean_db: psycopg.Connection, broker: ActionBroker, task_id: str
) -> None:
    """The log never claims less than what happened: intent, then outcome."""
    outcome = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="nar.stage_cart",
        payload={"supplier": "nar"},
        schedule_slot=SLOT,
    )
    with entity_session(clean_db, "ithrive"):
        rows = clean_db.execute(
            "SELECT phase FROM audit_log WHERE proposal_id = %s ORDER BY at, id",
            (outcome.proposal_id,),
        ).fetchall()
    assert [r[0] for r in rows] == ["intent", "outcome"]


def test_a_capability_the_supplier_lacks_is_refused_before_policy_runs(
    clean_db: psycopg.Connection, task_id: str
) -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_type="supplier.stage_cart",
            capability="stage_cart",
            category="purchase",
            reversible="yes",
            executor=lambda conn, entity_id, payload: {"staged": True},
        )
    )
    capability_broker = ActionBroker(
        registry=registry,
        policy=PolicyEngine.load(CONFIG, "ithrive"),
        supplier_capabilities={"world_richman": ["report_only"]},
    )
    with pytest.raises(BrokerRefusalError) as excinfo:
        capability_broker.propose(
            clean_db,
            entity_id="ithrive",
            task_id=task_id,
            action_type="supplier.stage_cart",
            payload={"supplier": "world_richman"},
            schedule_slot=SLOT,
        )
    assert "stage_cart" in str(excinfo.value)
    with entity_session(clean_db, "ithrive"):
        count = clean_db.execute("SELECT count(*) FROM action_proposals").fetchone()
    assert count == (0,)  # refused before anything was written or decided


def test_re_running_the_same_slot_does_not_duplicate_work(
    clean_db: psycopg.Connection, broker: ActionBroker, task_id: str, tmp_path: Path
) -> None:
    payload = {
        "content": "# A report\n",
        "out_dir": str(tmp_path),
        "kind": "shannon.replenishment",
        "schedule_slot": SLOT,
        "bom_version": "test",
        "config_snapshot": json.dumps({}),
        "task_id": task_id,
    }
    first = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="internal.write_draft_report",
        payload=payload,
        schedule_slot=SLOT,
    )
    second = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="internal.write_draft_report",
        payload=payload,
        schedule_slot=SLOT,
    )
    assert second.duplicate is True
    assert second.proposal_id == first.proposal_id
    with entity_session(clean_db, "ithrive"):
        count = clean_db.execute("SELECT count(*) FROM action_proposals").fetchone()
    assert count == (1,)


def test_an_explicit_attempt_salt_allows_a_deliberate_correction(
    clean_db: psycopg.Connection, broker: ActionBroker, task_id: str, tmp_path: Path
) -> None:
    payload = {
        "content": "# A corrected report\n",
        "out_dir": str(tmp_path),
        "kind": "shannon.replenishment",
        "schedule_slot": SLOT,
        "bom_version": "test",
        "config_snapshot": json.dumps({}),
        "task_id": task_id,
    }
    first = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="internal.write_draft_report",
        payload=payload,
        schedule_slot=SLOT,
    )
    second = broker.propose(
        clean_db,
        entity_id="ithrive",
        task_id=task_id,
        action_type="internal.write_draft_report",
        payload=payload,
        schedule_slot=SLOT,
        attempt_salt="zach-asked-for-a-rerun",
    )
    assert second.duplicate is False
    assert second.proposal_id != first.proposal_id


def test_only_tier_zero_executors_are_registered_in_this_phase(broker: ActionBroker) -> None:
    """Nothing that sends, buys or browses is even wired up."""
    assert broker.registry.action_types() == ["internal.write_draft_report"]
