"""The ActionBroker — the single doorway to any effect outside Shannon's head.

Order of checks, deliberately: capability first (can this supplier be
touched this way at all?), then policy (what does that cost in approval?),
then the phase ceiling (is this phase allowed to do it?), and only then
execution — write-ahead audited on both sides.

Phase 1 sets `max_tier_this_phase: 0` in `config/policy/global.yaml`.
Anything above Tier 0 is refused here, at the doorway, whatever the rest of
the system thinks it wants. Raising that number is the deliberate act that
opens the next phase; nothing else in this file changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from agent_org.audit.log import AuditLog
from agent_org.broker.proposals import ProposalStatus, fingerprint
from agent_org.broker.registry import ExecutorRegistry
from agent_org.config.models import Capability, Supplier
from agent_org.policy.engine import ActionContext, PolicyEngine, TrailingHistory


class BrokerRefusal(Exception):
    """Raised when the broker will not carry out an action.

    Refusal is a normal, expected outcome — not a crash. The caller reports
    it; the audit log keeps it.
    """

    def __init__(self, action_type: str, reason: str, tier: int | None = None) -> None:
        self.action_type = action_type
        self.reason = reason
        self.tier = tier
        super().__init__(f"{action_type} refused: {reason}")


@dataclass(frozen=True)
class BrokerOutcome:
    action_type: str
    proposal_id: str
    tier: int
    status: ProposalStatus
    result: dict[str, Any]
    reasons: tuple[str, ...]
    duplicate_of: str | None = None


class ActionBroker:
    def __init__(
        self,
        conn: psycopg.Connection[tuple[object, ...]],
        entity_id: str,
        policy: PolicyEngine,
        registry: ExecutorRegistry,
        audit: AuditLog,
        suppliers: dict[str, Supplier] | None = None,
    ) -> None:
        self.conn = conn
        self.entity_id = entity_id
        self.policy = policy
        self.registry = registry
        self.audit = audit
        self.suppliers = suppliers or {}

    def submit(
        self,
        action_type: str,
        payload: dict[str, Any],
        task_id: str,
        schedule_slot: str,
        data_snapshot_at: datetime | None = None,
        context: ActionContext | None = None,
        history: TrailingHistory | None = None,
        attempt_salt: str = "",
    ) -> BrokerOutcome:
        snapshot = data_snapshot_at or datetime.now(tz=UTC)
        intent = self.audit.intent(
            "proposal.file",
            {"action_type": action_type, "schedule_slot": schedule_slot},
            task_id=task_id,
        )

        executor = self.registry.get(action_type)
        if executor is None:
            reason = (
                f"There is no executor for '{action_type}'. Nothing in this phase can "
                "carry that out."
            )
            self.audit.outcome(intent, {"refused": reason}, task_id=task_id)
            raise BrokerRefusal(action_type, reason)

        self._check_capability(
            executor.supplier,
            executor.requires_capability,
            action_type,
            intent,
            task_id,
        )

        ctx = context or ActionContext(reversible=executor.reversible, category=executor.category)
        decision = self.policy.resolve(action_type, ctx, history)

        if decision.tier > self.policy.max_tier_this_phase:
            reason = (
                f"'{action_type}' is tier {decision.tier}. This phase is read-only and "
                f"allows tier {self.policy.max_tier_this_phase} only "
                f"({'; '.join(decision.reasons)})."
            )
            self.audit.outcome(
                intent,
                {"refused": reason, "tier": decision.tier},
                task_id=task_id,
            )
            raise BrokerRefusal(action_type, reason, tier=decision.tier)

        salt = self._salt_for(attempt_salt, decision.tier, action_type, task_id)
        key = fingerprint(self.entity_id, action_type, payload, schedule_slot, salt)
        existing = self._find_by_key(key)
        if existing is not None:
            proposal_id, status, result = existing
            self.audit.outcome(
                intent,
                {"duplicate_of": proposal_id, "status": status},
                task_id=task_id,
                proposal_id=proposal_id,
            )
            return BrokerOutcome(
                action_type=action_type,
                proposal_id=proposal_id,
                tier=decision.tier,
                status=ProposalStatus(status),
                result=result or {},
                reasons=decision.reasons,
                duplicate_of=proposal_id,
            )

        proposal_id = self._insert_proposal(
            task_id, action_type, payload, snapshot, decision.tier, executor.reversible, key
        )
        self.audit.outcome(
            intent,
            {"tier": decision.tier, "reasons": list(decision.reasons)},
            task_id=task_id,
            proposal_id=proposal_id,
        )

        execute_intent = self.audit.intent(
            "proposal.status",
            {"to": ProposalStatus.EXECUTING.value},
            task_id=task_id,
            proposal_id=proposal_id,
        )
        self._set_status(proposal_id, ProposalStatus.EXECUTING)
        try:
            result = executor.run(payload)
        except Exception as exc:
            self._set_status(proposal_id, ProposalStatus.FAILED)
            self.audit.outcome(
                execute_intent,
                {"status": ProposalStatus.FAILED.value, "error": str(exc)},
                task_id=task_id,
                proposal_id=proposal_id,
            )
            raise
        self._set_status(proposal_id, ProposalStatus.EXECUTED, result)
        self.audit.outcome(
            execute_intent,
            {"status": ProposalStatus.EXECUTED.value},
            task_id=task_id,
            proposal_id=proposal_id,
        )
        return BrokerOutcome(
            action_type=action_type,
            proposal_id=proposal_id,
            tier=decision.tier,
            status=ProposalStatus.EXECUTED,
            result=result,
            reasons=decision.reasons,
        )

    def _salt_for(
        self,
        attempt_salt: str,
        tier: int,
        action_type: str,
        task_id: str,
    ) -> str:
        """A deliberate repeat may repeat a Tier 0 action, and nothing else.

        `shannon run --again` re-reads and re-reports, and a report is a file
        on Zach's own machine. Everything above Tier 0 has an effect he cannot
        take back — a cart staged twice, an email sent twice — so the salt is
        dropped there and the fingerprint stays exactly what it was. The rule
        lives at the doorway rather than in the caller, because the caller is
        the thing most likely to be wrong.
        """
        if not attempt_salt or tier <= 0:
            return attempt_salt
        ignored = self.audit.intent(
            "proposal.fingerprint",
            {
                "action_type": action_type,
                "attempt_salt_ignored": attempt_salt,
                "tier": tier,
            },
            task_id=task_id,
        )
        self.audit.outcome(
            ignored,
            {
                "kept_original_fingerprint": True,
                "reason": (
                    "A re-run may regenerate a report. It may not repeat an action "
                    "with an effect outside this machine, so this one keeps its "
                    "original fingerprint and happens exactly once."
                ),
            },
            task_id=task_id,
        )
        return ""

    def _check_capability(
        self,
        supplier_key: str | None,
        required: Capability | None,
        action_type: str,
        intent: Any,
        task_id: str,
    ) -> None:
        if supplier_key is None or required is None:
            return
        supplier = self.suppliers.get(supplier_key)
        if supplier is None or not supplier.can(required):
            reason = (
                f"{supplier_key} is not set up for '{required.value}', so "
                f"'{action_type}' is not allowed for its lines."
            )
            self.audit.outcome(intent, {"refused": reason}, task_id=task_id)
            raise BrokerRefusal(action_type, reason)

    def _find_by_key(self, key: str) -> tuple[str, str, dict[str, Any] | None] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, result FROM action_proposals WHERE idempotency_key = %s",
                (key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        result = row[2] if isinstance(row[2], dict) else None
        return str(row[0]), str(row[1]), result

    def _insert_proposal(
        self,
        task_id: str,
        action_type: str,
        payload: dict[str, Any],
        snapshot: datetime,
        tier: int,
        reversible: str,
        key: str,
    ) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO action_proposals (entity_id, task_id, action_type, payload,
                                              data_snapshot_at, tier, reversible, status,
                                              idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'PROPOSED', %s)
                RETURNING id
                """,
                (
                    self.entity_id,
                    task_id,
                    action_type,
                    json.dumps(payload, default=str),
                    snapshot,
                    tier,
                    reversible,
                    key,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return str(row[0])

    def _set_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        result: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE action_proposals
               SET status = %s,
                   result = COALESCE(%s::jsonb, result),
                   executed_at = CASE WHEN %s = 'EXECUTED' THEN now() ELSE executed_at END
             WHERE id = %s
            """,
            (
                status.value,
                json.dumps(result, default=str) if result is not None else None,
                status.value,
                proposal_id,
            ),
        )


__all__ = ["ActionBroker", "BrokerOutcome", "BrokerRefusal"]
