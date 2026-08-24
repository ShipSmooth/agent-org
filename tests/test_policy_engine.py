"""PolicyEngine — data-driven, default-deny (docs/policy.md)."""

from __future__ import annotations

from pathlib import Path

from agent_org.policy.engine import ActionContext, PolicyEngine

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _engine() -> PolicyEngine:
    return PolicyEngine.load(CONFIG, "ithrive")


def test_known_reads_are_tier_zero() -> None:
    engine = _engine()
    assert engine.resolve("veeqo.read_inventory", ActionContext()).tier == 0
    assert engine.resolve("internal.write_draft_report", ActionContext()).tier == 0


def test_unknown_action_defaults_to_tier_three() -> None:
    """An action nobody wrote a rule for is refused, not permitted."""
    resolution = _engine().resolve("nar.checkout_and_pay", ActionContext())
    assert resolution.tier == 3
    assert resolution.matched_rule == "default_deny"


def test_an_irreversible_action_escalates_whatever_its_rule_says() -> None:
    resolution = _engine().resolve("internal.state_write", ActionContext(reversible="no"))
    assert resolution.tier == 3
    assert "irreversible" in resolution.fired_triggers


def test_purchase_paths_are_tier_two() -> None:
    engine = _engine()
    assert engine.resolve("nar.stage_cart", ActionContext()).tier == 2
    assert engine.resolve("notify.email", ActionContext()).tier == 2


def test_anomalous_total_escalates_to_tier_three() -> None:
    engine = _engine()
    ctx = ActionContext(
        category="purchase",
        total_usd=90000.0,
        trailing_order_count=8,
        trailing_avg_total_usd=5000.0,
    )
    resolution = engine.resolve("nar.stage_cart", ctx)
    assert resolution.tier == 3
    assert resolution.fired_triggers


def test_a_normal_order_does_not_escalate() -> None:
    engine = _engine()
    ctx = ActionContext(
        category="purchase",
        total_usd=6000.0,
        total_units=400,
        trailing_order_count=8,
        trailing_avg_total_usd=5500.0,
        trailing_avg_total_units=380.0,
        max_line_qty_vs_trailing_avg=1.1,
    )
    resolution = engine.resolve("nar.stage_cart", ctx)
    assert resolution.tier == 2
    assert resolution.fired_triggers == ()


def test_thin_history_is_tier_three_by_construction() -> None:
    """Under min_history_orders the ratios mean nothing, so Zach confirms (§policy)."""
    engine = _engine()
    ctx = ActionContext(category="purchase", total_usd=6000.0, trailing_order_count=1)
    resolution = engine.resolve("nar.stage_cart", ctx)
    assert resolution.tier == 3
    assert any("history" in trigger for trigger in resolution.fired_triggers)


def test_entity_policy_can_never_lower_a_global_tier(tmp_path: Path) -> None:
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "global.yaml").write_text(
        "default_tier: 3\nrules:\n  - {action: nar.stage_cart, tier: 2}\n", encoding="utf-8"
    )
    (tmp_path / "ithrive").mkdir()
    (tmp_path / "ithrive" / "policy.yaml").write_text(
        "rules:\n  - {action: nar.stage_cart, tier: 0}\n", encoding="utf-8"
    )
    engine = PolicyEngine.load(tmp_path, "ithrive")
    assert engine.resolve("nar.stage_cart", ActionContext()).tier == 2
