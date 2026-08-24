"""Shannon's weekly report — a readable file, written, never emailed.

Contains everything docs/replenishment.md requires: per line the raw net
requirement → MOQ-rounded → nearest-5 → purchase units → actual units;
the parameter values used; the bom_version; the config diff since the
last run; the gap list; build recommendations with limiting components;
and the persistent parking lot.
"""

from __future__ import annotations

import difflib
import json
from fractions import Fraction

from agent_org.shannon.calculator import RunResult
from agent_org.shannon.config_model import EntityConfig
from agent_org.shannon.configload import Issue


def _num(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{float(value):.2f}"


def config_change_summary(cfg: EntityConfig, previous_snapshot: str | None) -> str:
    """One line, printed at the top of every report."""
    if previous_snapshot is None:
        return "Config changes since last run: first run — no previous config to compare."
    try:
        previous = json.loads(previous_snapshot)
    except json.JSONDecodeError:
        return "Config changes since last run: previous snapshot unreadable — treat as changed."
    if not isinstance(previous, dict):
        return "Config changes since last run: previous snapshot unreadable — treat as changed."
    changed = sorted(
        name
        for name in set(previous) | set(cfg.config_texts)
        if previous.get(name) != cfg.config_texts.get(name)
    )
    if not changed:
        return "Config changes since last run: none."
    return f"Config changes since last run: {', '.join(changed)} changed."


def config_diff(cfg: EntityConfig, previous_snapshot: str | None) -> str:
    if previous_snapshot is None:
        return "(first run — nothing to diff)"
    try:
        previous = json.loads(previous_snapshot)
    except json.JSONDecodeError:
        return "(previous snapshot unreadable)"
    if not isinstance(previous, dict):
        return "(previous snapshot unreadable)"
    chunks: list[str] = []
    for name in sorted(set(previous) | set(cfg.config_texts)):
        old = str(previous.get(name, ""))
        new = cfg.config_texts.get(name, "")
        if old == new:
            continue
        diff = difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{name} (last run)",
            tofile=f"{name} (now)",
            lineterm="",
            n=1,
        )
        chunks.append("\n".join(diff))
    return "\n\n".join(chunks) if chunks else "(no changes)"


def render_report(
    cfg: EntityConfig,
    result: RunResult,
    *,
    schedule_slot: str,
    warnings: list[Issue],
    previous_snapshot: str | None,
) -> str:
    p = cfg.shannon.params
    lines: list[str] = []
    add = lines.append

    add(f"# Shannon — weekly replenishment report ({cfg.legal_name})")
    add("")
    add(f"Run slot: {schedule_slot}")
    add(f"BOM version: {cfg.bom_version}")
    add(config_change_summary(cfg, previous_snapshot))
    add("")
    add(
        "This report was computed from read-only data and written to this file "
        "and the database. Nothing was sent, staged, bought or changed anywhere "
        "— Phase 1 builds the brain, not the hands."
    )
    add("")

    add("## Parameters used")
    add("")
    add(f"- velocity_window_days: {p.velocity_window_days}")
    add(f"- cover_target_weeks: {_num(p.cover_target_weeks)} (inclusive of lead time)")
    add(f"- safety_stock_weeks: {_num(p.safety_stock_weeks)}")
    add(f"- round_up_to_nearest: {p.round_up_to_nearest}")
    add(f"- mf_floor_weeks: {_num(p.mf_floor_weeks)}")
    add(f"- fba_cover_weeks: {_num(p.fba_cover_weeks)}")
    add(f"- walmart_reserve_units: {p.walmart_reserve_units}")
    add(f"- box_min/box_max: {p.box_min}/{p.box_max}")
    add("")

    add("## Forecast purchase lines")
    add("")
    add(
        "Each line shows every rounding stage: raw net requirement → "
        "MOQ-rounded → nearest-5 (order units, sellable) → purchase units → "
        "actual units. Purchase units are what would go in a cart — never "
        "order units."
    )
    add("")
    add(
        "| Component | Supplier | Gross | On hand | On order | In transit | "
        "Raw net | MOQ-rounded | Nearest-5 | Pack size | Purchase units | Actual units |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for line in result.order_lines:
        add(
            f"| {line.name} ({line.key}) | {line.key.supplier} | "
            f"{_num(line.gross_demand)} | {line.on_hand} | {line.on_order} | "
            f"{line.in_transit} | {_num(line.net_requirement)} | {line.moq_rounded} | "
            f"{line.order_units} | {line.units_per_purchase_unit} | "
            f"{line.purchase_units} | {line.actual_units} |"
        )
    add("")
    for line in result.order_lines:
        for flag in line.flags:
            add(f"- {line.name} ({line.key}): {flag}")
    add("")

    add("## Non-stocked components (always zero)")
    add("")
    if result.non_stocked:
        for ns in result.non_stocked:
            add(f"- {ns.name} ({ns.key}): purchase 0 — {ns.note}")
    else:
        add("None in these BOMs.")
    add("")

    add("## Threshold top-ups (reorder_point components)")
    add("")
    if result.top_ups:
        add("| Component | Available | Reorder point | Target | Top-up | Route |")
        add("|---|---|---|---|---|---|")
        for t in result.top_ups:
            add(
                f"| {t.name} ({t.key}) | {t.available} | {t.reorder_point} | "
                f"{t.reorder_target} | {t.top_up} | {t.routing} |"
            )
        add("")
        for t in result.top_ups:
            for flag in t.flags:
                add(f"- {t.name} ({t.key}): {flag}")
    else:
        add("Nothing is below its reorder point.")
    add("")

    add("## Build recommendations")
    add("")
    add("Assembly labour is human-planned: these are recommendations, never actions.")
    add("")
    add("| Kit | Demand (H weeks) | Assembled stock | Build | Feasible now | Limiting component |")
    add("|---|---|---|---|---|---|")
    for b in result.builds:
        feasible = "unlimited" if b.feasible_units is None else str(b.feasible_units)
        add(
            f"| {b.name} ({b.kit_group}) | {b.demand} | {b.assembled} | {b.build} | "
            f"{feasible} | {b.limiting_component or '—'} |"
        )
    add("")
    for b in result.builds:
        if b.blocked_note:
            add(f"- {b.name}: {b.blocked_note}")
    add("")

    add("## Channel allocation")
    add("")
    add(
        "| SKU / kit | Warehouse | MF floor | Allocatable | FBA target | "
        "FBA on hand | FBA inbound | Send to FBA |"
    )
    add("|---|---|---|---|---|---|---|---|")
    for a in result.allocations:
        add(
            f"| {a.sku} | {a.warehouse_on_hand} | {a.mf_floor} | {a.allocatable} | "
            f"{a.fba_target} | {a.fba_on_hand} | {a.fba_inbound} | {a.fba_send} |"
        )
    add("")

    add("## FBA box plan")
    add("")
    if result.box_plan is None:
        add("Nothing to send to FBA this run.")
    else:
        bp = result.box_plan
        add(f"{bp.boxes} boxes, every box packed identically (total error {bp.total_error}).")
        add("")
        add("| SKU | Target | Per box | Planned | Over/short |")
        add("|---|---|---|---|---|")
        for bl in bp.lines:
            add(f"| {bl.sku} | {bl.target} | {bl.per_box} | {bl.planned} | {bl.delta:+d} |")
    add("")

    if result.prep_need:
        add("## FBA prep consumption")
        add("")
        add("Consumed against units sent to FBA, not against total sales.")
        add("")
        for key, need in sorted(result.prep_need.items(), key=lambda kv: str(kv[0])):
            comp = cfg.components.get(key)
            name = comp.name if comp else str(key)
            add(f"- {name} ({key}): {need}")
        add("")

    add("## Gap list")
    add("")
    if result.gap_list:
        for g in result.gap_list:
            detail = ""
            if g.available is not None and g.threshold is not None:
                detail = f" (available {g.available}, threshold {g.threshold})"
            topup = f" Suggested top-up: {g.suggested_top_up}." if g.suggested_top_up else ""
            add(f"- {g.name} ({g.key}): {g.reason}.{detail}{topup}")
    else:
        add("No gaps this run.")
    add("")

    if result.flags:
        add("## Flags")
        add("")
        for flag in result.flags:
            add(f"- {flag}")
        add("")

    if warnings:
        add("## Config warnings")
        add("")
        for w in warnings:
            add(f"- {w.loc}: {w.message}")
        add("")

    add("## Parking lot")
    add("")
    add("Carried in every report until Zach clears each item. Never silently shrinks.")
    add("")
    for item in cfg.parking_lot:
        blocks = f" Blocks: {item.blocks}." if item.blocks else ""
        add(f"- {item.pl_id}: {item.item}.{blocks}")
    add("")

    add("## Config diff since last run")
    add("")
    add("```diff")
    add(config_diff(cfg, previous_snapshot))
    add("```")
    add("")
    add("— Shannon")
    return "\n".join(lines)
