"""The executor registry — the shape of everything that can *do* something.

The implementations live in `agent_org.broker.executors`, which an
import-linter contract stops Shannon importing at all. She files a
proposal; the broker decides; an executor acts. This module holds only the
description of an executor, so the broker can be typed without dragging
the outside world in behind it.

In Phase 1 exactly one executor is ever registered, and it writes a file
and a database row. There is no executor for staging a cart, sending an
email or sending an SMS: those are not "disabled by a flag", they do not
exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent_org.config.models import Capability


class ExecutorFn(Protocol):
    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Executor:
    action_type: str
    reversible: str  # yes | no | window
    category: str  # internal | purchase | notify | read
    supplier: str | None
    requires_capability: Capability | None
    run: ExecutorFn


class ExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}

    def register(self, executor: Executor) -> None:
        self._executors[executor.action_type] = executor

    def get(self, action_type: str) -> Executor | None:
        return self._executors.get(action_type)

    def action_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))


__all__ = ["Executor", "ExecutorFn", "ExecutorRegistry"]
