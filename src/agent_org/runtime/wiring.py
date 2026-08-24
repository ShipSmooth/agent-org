"""Composition root — the one place the executor registry is wired up.

Shannon (and any agent) never imports ``agent_org.broker.executors``;
the CLI builds the broker here and hands it in.
"""

from __future__ import annotations

from pathlib import Path

from agent_org.broker.broker import ActionBroker
from agent_org.broker.executors import build_registry
from agent_org.policy.engine import PolicyEngine
from agent_org.shannon.config_model import EntityConfig


def supplier_capabilities(cfg: EntityConfig) -> dict[str, list[str]]:
    caps: dict[str, list[str]] = {}
    for key, sup in cfg.suppliers.items():
        if sup.acquisition == "browser":
            caps[key] = ["stage_cart"]
        elif sup.acquisition == "cart_url":
            caps[key] = ["cart_url"]
        elif sup.acquisition == "purchase_order":
            caps[key] = ["report_only"]
        else:
            caps[key] = []
    return caps


def build_broker(cfg: EntityConfig, config_dir: Path) -> ActionBroker:
    return ActionBroker(
        registry=build_registry(),
        policy=PolicyEngine.load(config_dir, cfg.entity_id),
        supplier_capabilities=supplier_capabilities(cfg),
    )
