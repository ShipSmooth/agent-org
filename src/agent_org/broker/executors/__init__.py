"""Executors — the hands. Only the broker may call these.

Agents must never import this package (import-linter enforces it).
Phase 1 registers exactly one executor: writing the draft report, a
Tier 0 internal action with no external effect.
"""

from __future__ import annotations

from agent_org.broker.actions import ActionRegistry, ActionSpec
from agent_org.broker.executors.internal import write_draft_report


def build_registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_type="internal.write_draft_report",
            category="internal",
            reversible="yes",
            capability=None,
            executor=write_draft_report,
        )
    )
    return registry
