"""Action proposals and their fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class ProposalStatus(str, Enum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    APPROVED = "APPROVED"
    APPROVED_AUTO = "APPROVED_AUTO"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVERSED = "REVERSED"


@dataclass(frozen=True)
class ActionProposal:
    id: str
    entity_id: str
    task_id: str
    action_type: str
    payload: dict[str, Any]
    data_snapshot_at: datetime
    tier: int
    reversible: str
    status: ProposalStatus
    idempotency_key: str
    result: dict[str, Any] | None = None


def canonical(payload: dict[str, Any]) -> str:
    """A stable text form of the payload, so the same request always hashes the same."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(
    entity_id: str,
    action_type: str,
    payload: dict[str, Any],
    schedule_slot: str,
    attempt_salt: str = "",
) -> str:
    """Identify a business action, not an attempt at it.

    A crashed run that re-files the same proposal produces the same key and
    is refused a second time. A deliberate re-run passes an `attempt_salt`,
    which visibly changes the key: duplicates are impossible by accident
    and possible only on purpose.
    """
    material = "\x1f".join(
        [entity_id, action_type, canonical(payload), schedule_slot, attempt_salt]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = ["ActionProposal", "ProposalStatus", "canonical", "fingerprint"]
