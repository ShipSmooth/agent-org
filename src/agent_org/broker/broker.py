"""ActionBroker — the single doorway for anything with an effect.

Every proposal is written to the database (write-ahead) with an audit
``intent`` row before anything runs, and updated with an ``outcome`` row
after. Idempotency: a SHA-256 fingerprint of (entity, action, canonical
payload, schedule slot) is a unique key, so a re-run cannot duplicate
work — the existing proposal is returned instead of executing again.

Phase 1 gate: this build is the brain without the hands. Only Tier 0
actions may execute; anything Tier 1 or above is REJECTED with a plain
reason. The policy machinery underneath is real — the gate sits on top
of it, it does not replace it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from agent_org.broker.actions import ActionRegistry
from agent_org.policy.engine import ActionContext, PolicyEngine, Resolution
from agent_org.runtime.audit import audit
from agent_org.tenancy.session import entity_session

PHASE = 1
MAX_EXECUTABLE_TIER_THIS_PHASE = 0


class BrokerRefusalError(RuntimeError):
    """The broker refused the action; the message says why in plain English."""


@dataclass(frozen=True)
class ProposalOutcome:
    proposal_id: str
    status: str  # 'EXECUTED' | 'REJECTED' | ...
    tier: int
    result: dict[str, object] | None
    duplicate: bool  # True when idempotency returned an existing proposal


def fingerprint(
    entity_id: str, action_type: str, payload: dict[str, object], schedule_slot: str
) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    material = "\x1f".join((entity_id, action_type, canonical, schedule_slot))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class ActionBroker:
    registry: ActionRegistry
    policy: PolicyEngine
    supplier_capabilities: dict[str, list[str]]  # supplier key -> capabilities

    def propose(
        self,
        conn: psycopg.Connection,
        *,
        entity_id: str,
        task_id: str,
        action_type: str,
        payload: dict[str, object],
        schedule_slot: str,
        context: ActionContext | None = None,
        attempt_salt: str = "",
    ) -> ProposalOutcome:
        spec = self.registry.get(action_type)

        # 1. capability check — before policy resolution (docs/policy.md).
        supplier = payload.get("supplier")
        if (
            spec is not None
            and spec.capability is not None
            and isinstance(supplier, str)
            and spec.capability not in self.supplier_capabilities.get(supplier, [])
        ):
            raise BrokerRefusalError(
                f"Refused: supplier '{supplier}' does not have the "
                f"'{spec.capability}' capability, so '{action_type}' cannot be "
                "performed for it. Capabilities are data; this is not a bug."
            )

        # 2. policy resolution (default-deny for unknown actions).
        ctx = context or ActionContext(
            category=spec.category if spec else None,
            reversible=spec.reversible if spec else "no",
        )
        resolution: Resolution = self.policy.resolve(action_type, ctx)

        slot_key = schedule_slot + (f"#{attempt_salt}" if attempt_salt else "")
        key = fingerprint(entity_id, action_type, payload, slot_key)

        with entity_session(conn, entity_id):
            # 3. write-ahead: the proposal row and its audit intent exist
            #    before anything is decided or executed.
            row = conn.execute(
                """
                INSERT INTO action_proposals
                    (entity_id, task_id, action_type, payload, data_snapshot_at,
                     tier, fired_triggers, reversible, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
                    entity_id,
                    task_id,
                    action_type,
                    json.dumps(payload),
                    datetime.now(tz=UTC),
                    resolution.tier,
                    json.dumps(list(resolution.fired_triggers)),
                    spec.reversible if spec else "no",
                    key,
                ),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT id, status, tier, result FROM action_proposals "
                    "WHERE idempotency_key = %s",
                    (key,),
                ).fetchone()
                assert existing is not None
                return ProposalOutcome(
                    proposal_id=str(existing[0]),
                    status=str(existing[1]),
                    tier=int(existing[2]),
                    result=existing[3] if isinstance(existing[3], dict) else None,
                    duplicate=True,
                )
            proposal_id = str(row[0])
            audit(
                conn,
                entity_id,
                actor="broker",
                event="proposal.status",
                phase="intent",
                task_id=task_id,
                proposal_id=proposal_id,
                detail={
                    "action_type": action_type,
                    "tier": resolution.tier,
                    "matched_rule": resolution.matched_rule,
                    "fired_triggers": list(resolution.fired_triggers),
                },
            )

            # 4. the Phase 1 gate: only Tier 0 ever executes in this build.
            if resolution.tier > MAX_EXECUTABLE_TIER_THIS_PHASE:
                reason = (
                    f"Phase {PHASE} is read-and-report only: '{action_type}' resolved "
                    f"to Tier {resolution.tier} (rule: {resolution.matched_rule}) and "
                    "nothing above Tier 0 may run. The proposal is recorded and "
                    "REJECTED, not executed."
                )
                self._decide(conn, entity_id, task_id, proposal_id, "REJECTED", reason)
                return ProposalOutcome(proposal_id, "REJECTED", resolution.tier, None, False)

            if spec is None:
                reason = (
                    f"'{action_type}' has no registered executor — nothing is wired to "
                    "perform it, so it cannot run."
                )
                self._decide(conn, entity_id, task_id, proposal_id, "REJECTED", reason)
                return ProposalOutcome(proposal_id, "REJECTED", resolution.tier, None, False)

            # 5. Tier 0: approved automatically, executed, outcome recorded.
            conn.execute(
                "UPDATE action_proposals SET status = 'EXECUTING', decided_at = now() "
                "WHERE id = %s",
                (proposal_id,),
            )
            try:
                result = spec.executor(conn, entity_id, payload)
            except Exception as exc:
                conn.execute(
                    "UPDATE action_proposals SET status = 'FAILED', result = %s WHERE id = %s",
                    (json.dumps({"error": str(exc)}), proposal_id),
                )
                audit(
                    conn,
                    entity_id,
                    actor="broker",
                    event="proposal.status",
                    phase="outcome",
                    task_id=task_id,
                    proposal_id=proposal_id,
                    detail={"status": "FAILED", "error": str(exc)},
                )
                raise
            conn.execute(
                "UPDATE action_proposals SET status = 'EXECUTED', executed_at = now(), "
                "result = %s WHERE id = %s",
                (json.dumps(result), proposal_id),
            )
            audit(
                conn,
                entity_id,
                actor="broker",
                event="proposal.status",
                phase="outcome",
                task_id=task_id,
                proposal_id=proposal_id,
                detail={"status": "EXECUTED"},
            )
            return ProposalOutcome(proposal_id, "EXECUTED", resolution.tier, result, False)

    def _decide(
        self,
        conn: psycopg.Connection,
        entity_id: str,
        task_id: str,
        proposal_id: str,
        status: str,
        reason: str,
    ) -> None:
        conn.execute(
            "UPDATE action_proposals SET status = %s, decided_at = now(), result = %s "
            "WHERE id = %s",
            (status, json.dumps({"reason": reason}), proposal_id),
        )
        audit(
            conn,
            entity_id,
            actor="broker",
            event="proposal.status",
            phase="outcome",
            task_id=task_id,
            proposal_id=proposal_id,
            detail={"status": status, "reason": reason},
        )
