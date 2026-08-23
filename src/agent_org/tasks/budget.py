"""Time and step budgets.

A task that hangs is worse than a task that fails: nobody notices silence.
Every run carries a wall-clock budget and a step budget; exceeding either
raises `BudgetExceeded`, which the runner turns into a FAILED task with the
reason on the record.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised when a task runs longer, or does more steps, than it is allowed."""


@dataclass
class Budget:
    wall_clock_seconds: float
    max_steps: int
    _clock: Callable[[], float] = field(default=time.monotonic)
    started_at: float = field(init=False)
    steps: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.started_at = self._clock()

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self.started_at

    def step(self, what: str) -> None:
        """Count one step of work and stop the run if either budget is spent."""
        self.steps += 1
        if self.steps > self.max_steps:
            raise BudgetExceeded(
                f"Stopped at step {self.steps} ('{what}'): the run is allowed "
                f"{self.max_steps} steps. Something is looping."
            )
        self.check_time(what)

    def check_time(self, what: str) -> None:
        elapsed = self.elapsed_seconds
        if elapsed > self.wall_clock_seconds:
            raise BudgetExceeded(
                f"Stopped during '{what}' after {elapsed:.0f} seconds: the run is "
                f"allowed {self.wall_clock_seconds:.0f} seconds."
            )


__all__ = ["Budget", "BudgetExceeded"]
