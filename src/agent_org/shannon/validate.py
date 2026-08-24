"""`shannon validate-config` — every check in docs/replenishment.md §13.

Each problem is reported in plain English with the file and line it came
from. Errors make the command exit non-zero; warnings do not. The
"every kit sold on any channel has a BOM entry" check needs sales data,
so it runs here only when sold SKUs are supplied (the run itself enforces
it as a hard failure either way).
"""

from __future__ import annotations

import re

from agent_org.shannon.config_model import (
    ASIN_SUPPLIER,
    BomLine,
    EntityConfig,
)
from agent_org.shannon.configload import Issue

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_CLASSES = {"forecast", "reorder_point", "non_stocked", "ops_consumable"}


def validate(cfg: EntityConfig, sold_skus: set[str] | None = None) -> list[Issue]:
    issues: list[Issue] = list(cfg.issues)
    channel_keys = cfg.channel_keys()

    # --- suppliers ---
    for sup in cfg.suppliers.values():
        if sup.lead_time_todo:
            issues.append(
                Issue(
                    "warning",
                    sup.loc,
                    f"Supplier '{sup.key}' has no lead time yet (marked TODO) — "
                    "parking-lot item PL-7. Zach needs to provide it.",
                )
            )

    # --- components ---
    for comp in cfg.components.values():
        if comp.cls is None:
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"Component {comp.key} has no 'class'. Every component must say "
                    "whether it is forecast, reorder_point, non_stocked or "
                    "ops_consumable — there is no default.",
                )
            )
        elif comp.cls not in _CLASSES:
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"Component {comp.key} has class '{comp.cls}', which is not one of "
                    "forecast, reorder_point, non_stocked, ops_consumable.",
                )
            )
        if comp.key.supplier.strip().upper() == "TODO":
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"Component {comp.key} has a pending supplier (still TODO) and a "
                    f"purchasable class ('{comp.cls}'). A pending supplier is fine until "
                    "the component needs buying; this one does, so Zach must say who "
                    "sells it before Shannon can route the line anywhere.",
                )
            )
        elif comp.key.supplier not in cfg.suppliers:
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"Component {comp.key} names supplier '{comp.key.supplier}', which is "
                    "not in the suppliers list. Add the supplier, or correct the spelling.",
                )
            )
        if comp.upu_missing:
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"Component {comp.key} has no 'units_per_purchase_unit'. Set the pack "
                    "size, or an explicit null while discovery mode awaits confirmation.",
                )
            )
        elif comp.units_per_purchase_unit is None and cfg.pack_size_mode != "discover_and_confirm":
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"Component {comp.key} has a null pack size but discovery mode is off — "
                    "a real units_per_purchase_unit is required.",
                )
            )
        if comp.key.supplier == ASIN_SUPPLIER and not _ASIN_RE.match(comp.key.part):
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"'{comp.key.part}' is not a valid Amazon ASIN — an ASIN is exactly "
                    "10 letters and digits.",
                )
            )
        if comp.purchase_asin is not None and not _ASIN_RE.match(comp.purchase_asin):
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"purchase_asin '{comp.purchase_asin}' on {comp.key} is not a valid "
                    "Amazon ASIN — an ASIN is exactly 10 letters and digits.",
                )
            )
        if comp.thresholds_todo:
            issues.append(
                Issue(
                    "warning",
                    comp.loc,
                    f"Component {comp.key} has TODO reorder thresholds — it will sit on "
                    "the gap list until Zach provides reorder_point and reorder_target.",
                )
            )
        if (
            comp.reorder_point is not None
            and comp.reorder_target is not None
            and comp.reorder_point > comp.reorder_target
        ):
            issues.append(
                Issue(
                    "warning",
                    comp.loc,
                    f"Component {comp.key} has reorder_point {comp.reorder_point} above "
                    f"reorder_target {comp.reorder_target} — the top-up would be negative.",
                )
            )
        # cover target must be >= supplier lead time (docs/replenishment.md §3)
        sup_cfg = cfg.suppliers.get(comp.key.supplier)
        if (
            comp.cls == "forecast"
            and sup_cfg is not None
            and sup_cfg.lead_time_weeks is not None
            and cfg.shannon.params.cover_target_weeks < sup_cfg.lead_time_weeks
        ):
            issues.append(
                Issue(
                    "error",
                    comp.loc,
                    f"cover_target_weeks ({cfg.shannon.params.cover_target_weeks}) is "
                    f"shorter than supplier '{sup_cfg.key}' lead time "
                    f"({sup_cfg.lead_time_weeks} weeks) for {comp.key} — orders would "
                    "always arrive too late.",
                )
            )

    # --- kits: BOM references, aliases, channels ---
    def check_line(line: BomLine, kit_group: str) -> None:
        if line.key not in cfg.components:
            issues.append(
                Issue(
                    "error",
                    line.loc,
                    f"Kit {kit_group} uses {line.key}, but no component with that "
                    "supplier and part number exists. Every BOM line must point at a "
                    "real component record (parking-lot PL-4 covers the instruction "
                    "cards).",
                )
            )
        if line.channels is not None:
            for ch in line.channels:
                if ch not in channel_keys:
                    issues.append(
                        Issue(
                            "error",
                            line.loc,
                            f"Kit {kit_group} restricts {line.key} to channel '{ch}', "
                            "which is not a configured channel for this entity.",
                        )
                    )

    for kit in cfg.kits.values():
        if not kit.components:
            issues.append(Issue("error", kit.loc, f"Kit {kit.kit_group} has no component lines."))
        for line in kit.components:
            check_line(line, kit.kit_group)
        if kit.pouch is not None:
            check_line(kit.pouch, kit.kit_group)
        for ch, sku in kit.aliases.items():
            loc = kit.alias_locs.get(ch, kit.loc)
            if ch not in channel_keys:
                issues.append(
                    Issue(
                        "error",
                        loc,
                        f"Kit {kit.kit_group} has an alias for channel '{ch}', which is "
                        "not a configured channel for this entity.",
                    )
                )
            if sku.strip().upper() == "TODO":
                issues.append(
                    Issue(
                        "error",
                        loc,
                        f"Kit {kit.kit_group} has no real SKU for its '{ch}' listing — "
                        "it is still TODO (parking-lot PL-8). Sales on that channel "
                        "could not be matched to this kit.",
                    )
                )

    # --- standalone FBA prep references ---
    for prep in cfg.standalone_prep:
        if prep.key not in cfg.components:
            issues.append(
                Issue(
                    "error",
                    prep.loc,
                    f"standalone_fba_prep consumes {prep.key}, but no such component exists.",
                )
            )
        if prep.sku is not None and prep.sku.strip().upper().endswith("TODO"):
            issues.append(
                Issue(
                    "warning",
                    prep.loc,
                    f"standalone_fba_prep row '{prep.sku}' is a placeholder SKU — the "
                    "real SKU is still needed.",
                )
            )

    # --- standalone products (fixtures / later syncs) ---
    for sp in cfg.standalone_products:
        if sp.key not in cfg.components:
            issues.append(
                Issue(
                    "error",
                    sp.loc,
                    f"Standalone product '{sp.sku}' points at {sp.key}, but no such "
                    "component exists.",
                )
            )
        if sp.sales_asin is not None and not _ASIN_RE.match(sp.sales_asin):
            issues.append(
                Issue(
                    "error",
                    sp.loc,
                    f"sales_asin '{sp.sales_asin}' on '{sp.sku}' is not a valid Amazon "
                    "ASIN — an ASIN is exactly 10 letters and digits.",
                )
            )

    # --- ops reminder groups: roles and classes ---
    for group in cfg.shannon.ops_groups:
        for role in group.recipients:
            if role not in cfg.shannon.recipients:
                issues.append(
                    Issue(
                        "error",
                        group.loc,
                        f"Ops-reminder group '{group.name}' sends to role '{role}', but "
                        "that role is not mapped to a real address in shannon.yaml. An "
                        "unmapped role is a config failure, never a silent drop.",
                    )
                )
        for key in group.components:
            group_comp = cfg.components.get(key)
            if group_comp is None:
                issues.append(
                    Issue(
                        "error",
                        group.loc,
                        f"Ops-reminder group '{group.name}' lists {key}, but no such "
                        "component exists.",
                    )
                )
            elif group_comp.cls != "ops_consumable":
                issues.append(
                    Issue(
                        "error",
                        group.loc,
                        f"Ops-reminder group '{group.name}' lists {key}, whose class is "
                        f"'{group_comp.cls}' — only ops_consumable components belong here.",
                    )
                )

    # --- every kit sold on any channel has a BOM entry (needs sales data) ---
    if sold_skus is not None:
        known = _known_sale_skus(cfg)
        for sku in sorted(sold_skus):
            if sku in known:
                continue
            if re.match(r"^\d{2}-\d{4}$", sku):
                # NAR pattern — a NAR item forecast directly as a purchasable
                # SKU (docs/replenishment.md §1), no explosion needed.
                continue
            issues.append(
                Issue(
                    "error",
                    cfg.kits[next(iter(cfg.kits))].loc if cfg.kits else cfg.shannon.loc,
                    f"SKU '{sku}' sold in the sales window but matches no kit alias, "
                    "no standalone product and no component — silently un-exploded "
                    "demand is the bug this check exists to prevent.",
                )
            )
    return issues


def _known_sale_skus(cfg: EntityConfig) -> set[str]:
    known: set[str] = set()
    for kit in cfg.kits.values():
        for sku in kit.aliases.values():
            if sku.strip().upper() != "TODO":
                known.add(sku)
    for sp in cfg.standalone_products:
        known.add(sp.sku)
    for comp in cfg.components.values():
        known.add(comp.key.part)
    return known
