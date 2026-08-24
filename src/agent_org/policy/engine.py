"""The policy engine — resolves an action to a tier.

Policy is data. This file contains no thresholds and no action names; it
reads `config/policy/global.yaml` plus the entity's overrides. An action
that matches no rule resolves to `default_tier`, which is 3: unknown means
maximum caution.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agent_org.config.models import PolicyConfig


@dataclass(frozen=True)
class ActionContext:
    """What the policy engine is allowed to consider about an action."""

    reversible: str = "yes"  # yes | no | window
    category: str = "internal"  # internal | purchase | notify | read
    total_usd: Decimal | None = None
    total_units: int | None = None
    line_quantities: tuple[int, ...] = ()


@dataclass(frozen=True)
class TrailingHistory:
    """Trailing order history, used only for the anomaly escalations."""

    order_count: int = 0
    average_total_usd: Decimal | None = None
    average_total_units: float | None = None
    average_line_quantity: dict[str, float] | None = None


@dataclass(frozen=True)
class PolicyDecision:
    action_type: str
    tier: int
    reasons: tuple[str, ...]

    @property
    def matched_a_rule(self) -> bool:
        return "no rule matched: default deny" not in self.reasons


class PolicyEngine:
    def __init__(self, policy: PolicyConfig) -> None:
        self.policy = policy

    @property
    def max_tier_this_phase(self) -> int:
        return self.policy.max_tier_this_phase

    def resolve(
        self,
        action_type: str,
        context: ActionContext | None = None,
        history: TrailingHistory | None = None,
    ) -> PolicyDecision:
        ctx = context or ActionContext()
        reasons: list[str] = []
        rule = self.policy.rules.get(action_type)
        if rule is None:
            tier = self.policy.default_tier
            reasons.append("no rule matched: default deny")
        else:
            tier = rule.tier
            reasons.append(f"rule '{action_type}' is tier {rule.tier}")

        if ctx.reversible == "no" and tier < 3:
            tier = 3
            reasons.append("the action cannot be undone")

        if ctx.category == "purchase":
            escalated, why = self._purchase_escalations(ctx, history or TrailingHistory())
            if escalated and tier < 3:
                tier = 3
            reasons.extend(why)

        return PolicyDecision(action_type=action_type, tier=tier, reasons=tuple(reasons))

    def _purchase_escalations(
        self, ctx: ActionContext, history: TrailingHistory
    ) -> tuple[bool, list[str]]:
        thresholds = self._thresholds()
        reasons: list[str] = []
        escalate = False

        absolute = thresholds.get("absolute_total_usd")
        if (
            ctx.total_usd is not None
            and absolute is not None
            and ctx.total_usd > Decimal(str(absolute))
        ):
            escalate = True
            reasons.append(f"the total is over ${absolute:,}")

        min_history = int(thresholds.get("min_history_orders", 4) or 4)
        if history.order_count < min_history:
            escalate = True
            reasons.append(
                f"there are only {history.order_count} past orders to compare "
                f"against ({min_history} are needed), so nothing can be called normal yet"
            )
            return escalate, reasons

        pct = thresholds.get("total_vs_trailing_avg_pct")
        if (
            ctx.total_usd is not None
            and history.average_total_usd is not None
            and pct is not None
            and ctx.total_usd > history.average_total_usd * Decimal(str(pct)) / 100
        ):
            escalate = True
            reasons.append(f"the total is more than {pct}% of the recent average")

        units_x = thresholds.get("total_units_vs_trailing_avg_x")
        if (
            ctx.total_units is not None
            and history.average_total_units
            and units_x is not None
            and ctx.total_units > history.average_total_units * float(units_x)
        ):
            escalate = True
            reasons.append(f"the number of units is more than {units_x}× the recent average")

        return escalate, reasons

    def _thresholds(self) -> dict[str, Any]:
        block = self.policy.thresholds.get("tier3_escalation")
        return dict(block) if isinstance(block, dict) else {}


__all__ = ["ActionContext", "PolicyDecision", "PolicyEngine", "TrailingHistory"]
