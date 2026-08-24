"""Action registry shared by the broker and its executors.

Agents import THIS module (and ``broker``), never
``agent_org.broker.executors`` — the import-linter contract enforces it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import psycopg

Executor = Callable[[psycopg.Connection, str, dict[str, object]], dict[str, object]]
# (conn, entity_id, payload) -> result


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    category: str  # 'read' | 'internal' | 'purchase' | 'notify' | ...
    reversible: str  # 'yes' | 'no' | 'window'
    capability: str | None  # required supplier capability, if supplier-bound
    executor: Executor


class ActionRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        self._specs[spec.action_type] = spec

    def get(self, action_type: str) -> ActionSpec | None:
        return self._specs.get(action_type)

    def action_types(self) -> list[str]:
        """Everything wired up in this build — the phase gate is visible here."""
        return sorted(self._specs)
