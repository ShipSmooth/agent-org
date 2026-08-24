"""Time and step budgets.

A task that hangs or loops must be killed and reported, never left
spinning. The budget is checked at every step boundary; external calls
additionally carry their own client timeouts, so a hung read surfaces at
the next boundary rather than running forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceededError(RuntimeError):
    """The task went over its time or step budget and was stopped."""


@dataclass
class TaskBudget:
    wall_seconds: float
    max_steps: int
    started_at: float = field(default_factory=time.monotonic)
    steps_taken: int = 0
    step_log: list[str] = field(default_factory=list)

    def step(self, name: str) -> None:
        """Record a step and enforce both budgets. Call at every boundary."""
        self.steps_taken += 1
        self.step_log.append(name)
        if self.steps_taken > self.max_steps:
            raise BudgetExceededError(
                f"Step budget exceeded: the task took more than {self.max_steps} steps "
                f"(stopped at step '{name}'). The run was killed and reported."
            )
        elapsed = time.monotonic() - self.started_at
        if elapsed > self.wall_seconds:
            raise BudgetExceededError(
                f"Time budget exceeded: the task ran longer than {self.wall_seconds:g} "
                f"seconds (stopped at step '{name}' after {elapsed:.1f}s). "
                "The run was killed and reported."
            )

    @property
    def wall_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)
