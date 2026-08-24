"""PolicyEngine — tiers from declarative YAML, default-deny.

Loads ``config/policy/global.yaml`` plus the per-entity override and
resolves a tier for every proposed action. Anything matching no rule
resolves to the default tier (3 — unknown means maximum caution).
Per-entity files may only override thresholds and add rules; they can
never lower a global rule's tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ActionContext:
    """Facts the escalation triggers look at. All optional; None = unknown."""

    category: str | None = None  # e.g. 'purchase'
    total_usd: float | None = None
    total_units: int | None = None
    reversible: str = "yes"  # 'yes' | 'no' | 'window'
    trailing_order_count: int = 0
    trailing_avg_total_usd: float | None = None
    trailing_avg_total_units: float | None = None
    max_line_qty_vs_trailing_avg: float | None = None


@dataclass(frozen=True)
class Resolution:
    tier: int
    matched_rule: str  # the action pattern that matched, or 'default_deny'
    fired_triggers: tuple[str, ...] = ()


@dataclass
class Thresholds:
    absolute_total_usd: float = 75000.0
    total_vs_trailing_avg_pct: float = 150.0
    line_qty_vs_trailing_avg_x: float = 2.0
    total_units_vs_trailing_avg_x: float = 2.0
    trailing_window_orders: int = 8
    min_history_orders: int = 4


@dataclass
class PolicyEngine:
    default_tier: int = 3
    rules: dict[str, int] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def load(cls, config_dir: Path, entity_id: str) -> PolicyEngine:
        engine = cls()
        global_path = config_dir / "policy" / "global.yaml"
        entity_path = config_dir / entity_id / "policy.yaml"
        if global_path.exists():
            engine._apply(yaml.safe_load(global_path.read_text(encoding="utf-8")), base=True)
        if entity_path.exists():
            engine._apply(yaml.safe_load(entity_path.read_text(encoding="utf-8")), base=False)
        return engine

    def _apply(self, doc: object, *, base: bool) -> None:
        if not isinstance(doc, dict):
            return
        if base and isinstance(doc.get("default_tier"), int):
            self.default_tier = doc["default_tier"]
        rules = doc.get("rules")
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                action = rule.get("action")
                tier = rule.get("tier")
                if not isinstance(action, str) or not isinstance(tier, int):
                    continue
                if not base and action in self.rules and tier < self.rules[action]:
                    # per-entity files can never lower a global rule's tier
                    continue
                self.rules[action] = tier
        thresholds = doc.get("thresholds")
        if isinstance(thresholds, dict):
            t3 = thresholds.get("tier3_escalation")
            if isinstance(t3, dict):
                for key in vars(self.thresholds):
                    value = t3.get(key)
                    if isinstance(value, int | float):
                        setattr(self.thresholds, key, value)

    def resolve(self, action_type: str, ctx: ActionContext) -> Resolution:
        if action_type in self.rules:
            tier = self.rules[action_type]
            matched = action_type
        else:
            tier = self.default_tier
            matched = "default_deny"
        fired = tuple(self._escalations(tier, ctx))
        if fired:
            tier = max(tier, 3)
        return Resolution(tier=tier, matched_rule=matched, fired_triggers=fired)

    def _escalations(self, tier: int, ctx: ActionContext) -> list[str]:
        fired: list[str] = []
        if ctx.reversible == "no":
            fired.append("irreversible")
        if ctx.category != "purchase":
            return fired
        t = self.thresholds
        if ctx.total_usd is not None and ctx.total_usd > t.absolute_total_usd:
            fired.append(f"total over ${t.absolute_total_usd:,.0f}")
        if ctx.trailing_order_count < t.min_history_orders:
            # comparative triggers cannot fire meaningfully without history:
            # any purchase action is Tier 3 until history exists.
            fired.append(f"fewer than {t.min_history_orders} past orders — no meaningful history")
            return fired
        if (
            ctx.total_usd is not None
            and ctx.trailing_avg_total_usd is not None
            and ctx.trailing_avg_total_usd > 0
            and ctx.total_usd > ctx.trailing_avg_total_usd * t.total_vs_trailing_avg_pct / 100.0
        ):
            fired.append(f"total over {t.total_vs_trailing_avg_pct:.0f}% of trailing average")
        if (
            ctx.max_line_qty_vs_trailing_avg is not None
            and ctx.max_line_qty_vs_trailing_avg > t.line_qty_vs_trailing_avg_x
        ):
            fired.append("a line quantity over 2x its trailing average")
        if (
            ctx.total_units is not None
            and ctx.trailing_avg_total_units is not None
            and ctx.trailing_avg_total_units > 0
            and ctx.total_units > ctx.trailing_avg_total_units * t.total_units_vs_trailing_avg_x
        ):
            fired.append("total units over 2x the trailing average")
        return fired
