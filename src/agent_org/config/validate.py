"""The checks behind `shannon validate-config` (docs/replenishment.md §13).

This command runs before every execution. Zach maintains the parts list by
hand; this is what makes that safe. Every message names a file and a line
and says what to do about it.

The committed BOM deliberately fails two of these checks (the
`own_printed / CARD-TODO` dangling reference and the `TODO` FBA aliases) —
they are real, open, parking-lot items, and a validator that passed them
would be lying.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from agent_org.config.errors import Finding, Severity, error, warning
from agent_org.config.models import (
    Capability,
    Component,
    ComponentClass,
    ComponentKey,
    LoadedConfig,
)
from agent_org.config.yamlsource import Loc

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")

PURCHASABLE_CLASSES = frozenset({ComponentClass.FORECAST, ComponentClass.REORDER_POINT})


@dataclass(frozen=True)
class ValidationResult:
    findings: tuple[Finding, ...]
    bom_version: str

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def blocking_errors(self) -> tuple[Finding, ...]:
        """Errors that make the whole run impossible, as opposed to the ones
        that spoil a single line and are reported as blocked."""
        return tuple(f for f in self.errors if f.blocks_run)

    @property
    def line_errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.errors if not f.blocks_run)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(config: LoadedConfig, extra: Sequence[Finding] | None = None) -> ValidationResult:
    findings: list[Finding] = list(extra or [])
    boms = config.boms
    entity = config.entity
    channel_keys = set(entity.channel_keys)

    findings += _check_suppliers(config)
    findings += _check_components(config)
    findings += _check_kits(config, channel_keys)
    findings += _check_fba_prep(config)
    findings += _check_recipients(config)

    return ValidationResult(findings=tuple(findings), bom_version=boms.bom_version)


def _check_suppliers(config: LoadedConfig) -> list[Finding]:
    findings: list[Finding] = []
    boms = config.boms
    for key, supplier in boms.suppliers.items():
        if not supplier.capabilities:
            findings.append(
                error(
                    f"Supplier '{key}' has no capabilities listed, so Shannon cannot tell "
                    "what she is allowed to do with its lines.",
                    supplier.loc,
                    fix=(
                        f"Add '{key}:' with a 'capabilities:' list to "
                        f"{config.entity.suppliers_config}."
                    ),
                )
            )
        if Capability.PURCHASE in supplier.capabilities:
            findings.append(
                error(
                    f"Supplier '{key}' is granted the 'purchase' capability. Nothing in "
                    "this system may buy anything.",
                    supplier.loc,
                    fix="Remove 'purchase' from that supplier's capabilities.",
                )
            )
    return findings


def _check_components(config: LoadedConfig) -> list[Finding]:
    findings: list[Finding] = []
    boms = config.boms
    for key, component in boms.components.items():
        if key.supplier not in boms.suppliers:
            findings.append(
                error(
                    f"Component {key.part} ({component.name}) names supplier "
                    f"'{key.supplier}', which is not one of the suppliers in this file.",
                    component.loc,
                    fix=(
                        "Add that supplier to the 'suppliers:' block, or correct the "
                        "supplier name. If it is genuinely undecided, use 'unsourced'."
                    ),
                    blocks_run=False,
                )
            )
        for label, asin in (
            ("purchase_asin", component.purchase_asin),
            ("part number", key.part if key.supplier == "amazon_business" else None),
        ):
            if asin is not None and not ASIN_PATTERN.match(asin):
                findings.append(
                    error(
                        f"Component {key} has an Amazon {label} of '{asin}'. An ASIN is "
                        "exactly 10 letters and digits.",
                        component.loc,
                        fix="Copy the ASIN from the Amazon listing URL and paste it here.",
                        blocks_run=False,
                    )
                )
        if component.component_class is ComponentClass.REORDER_POINT:
            findings += _check_reorder_thresholds(component.key, component)
        if component.component_class in PURCHASABLE_CLASSES:
            findings += _check_lead_time(config, key)
        if component.units_per_purchase_unit is not None and component.units_per_purchase_unit < 1:
            findings.append(
                error(
                    f"Component {key} says one purchase unit contains "
                    f"{component.units_per_purchase_unit} sellable units.",
                    component.loc,
                    fix="Use 1 for singles, or the real pack size.",
                )
            )
    return findings


def _check_reorder_thresholds(key: ComponentKey, component: Component) -> list[Finding]:
    findings: list[Finding] = []
    if component.reorder_point is None or component.reorder_target is None:
        findings.append(
            warning(
                f"Component {key} is a reorder-point item but its reorder point or target "
                "is still unresolved, so Shannon cannot say when it runs low.",
                component.loc,
                fix="Fill in 'reorder_point' and 'reorder_target' with numbers.",
            )
        )
        return findings
    if component.reorder_point > component.reorder_target:
        findings.append(
            warning(
                f"Component {key} would be topped up to {component.reorder_target}, which "
                f"is below the level of {component.reorder_point} that triggers the "
                "top-up — so it would be flagged again immediately.",
                component.loc,
                fix="Raise 'reorder_target' above 'reorder_point'.",
            )
        )
    return findings


def _check_lead_time(config: LoadedConfig, key: ComponentKey) -> list[Finding]:
    component = config.boms.components[key]
    supplier = config.boms.suppliers.get(key.supplier)
    if supplier is None or supplier.lead_time_weeks is None:
        return []
    cover: Fraction = component.cover_target_weeks or config.shannon.parameters.cover_target_weeks
    if cover < supplier.lead_time_weeks:
        return [
            error(
                f"Component {key} is covered for {cover} weeks, but {supplier.name} takes "
                f"{supplier.lead_time_weeks} weeks to deliver, so an order would arrive "
                "after the stock ran out.",
                component.loc,
                fix=(
                    "Raise 'cover_target_weeks' to at least the lead time. The cover "
                    "target includes the lead time; it is not added on top of it."
                ),
            )
        ]
    return []


def _check_kits(config: LoadedConfig, channel_keys: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    boms = config.boms
    seen_skus: dict[str, str] = {}
    for kit_group, kit in boms.kits.items():
        if not kit.lines:
            findings.append(error(f"Kit '{kit_group}' has no components listed.", kit.loc))
        if len(kit.lines) > 25:
            findings.append(
                warning(
                    f"Kit '{kit_group}' has {len(kit.lines)} component lines. Veeqo "
                    "refuses bundles over 25, so this kit cannot be mirrored there.",
                    kit.loc,
                )
            )
        for channel_key, sku in kit.aliases.items():
            if channel_key not in channel_keys:
                findings.append(
                    error(
                        f"Kit '{kit_group}' has a SKU for a channel called "
                        f"'{channel_key}', which is not a channel this business sells on.",
                        kit.loc,
                        fix=(
                            "Use one of: "
                            + ", ".join(sorted(channel_keys))
                            + f", or add the channel to {config.entity.entity_id}.yaml."
                        ),
                    )
                )
            if sku is None:
                findings.append(
                    error(
                        f"Kit '{kit_group}' has no SKU for the '{channel_key}' channel — "
                        "the file says TODO. Sales on that channel would not be counted, "
                        "so Shannon would under-order everything in this kit.",
                        kit.loc,
                        fix=(
                            "Export the channel SKUs from Seller Central and paste the "
                            "real value in (parking-lot item PL-8)."
                        ),
                        blocks_run=False,
                    )
                )
                continue
            if sku in seen_skus and seen_skus[sku] != kit_group:
                findings.append(
                    error(
                        f"SKU '{sku}' is claimed by both '{seen_skus[sku]}' and "
                        f"'{kit_group}'. Sales of it would be counted twice.",
                        kit.loc,
                    )
                )
            seen_skus[sku] = kit_group
        for line in kit.lines:
            if line.component.part in boms.kits:
                findings.append(
                    error(
                        f"Kit '{kit_group}' contains another kit "
                        f"('{line.component.part}'). Kits inside kits are not supported.",
                        line.loc,
                    )
                )
            if line.component not in boms.components:
                findings.append(
                    error(
                        f"Kit '{kit_group}' uses part '{line.component.part}' from "
                        f"'{line.component.supplier}', but there is no component with "
                        "that supplier and part number in this file.",
                        line.loc,
                        fix=(
                            "Add the component to the 'components:' block, or correct "
                            "the part number on this line."
                        ),
                        blocks_run=False,
                    )
                )
            if line.qty < 1:
                findings.append(
                    error(
                        f"Kit '{kit_group}' uses {line.qty} of "
                        f"'{line.component.part}'. A kit line needs a quantity of at "
                        "least 1.",
                        line.loc,
                    )
                )
            for channel_key in line.channels or ():
                if channel_key not in channel_keys:
                    findings.append(
                        error(
                            f"Kit '{kit_group}' limits part '{line.component.part}' to a "
                            f"channel called '{channel_key}', which is not a channel this "
                            "business sells on.",
                            line.loc,
                            fix="Use one of: " + ", ".join(sorted(channel_keys)) + ".",
                        )
                    )
    return findings


def _check_fba_prep(config: LoadedConfig) -> list[Finding]:
    findings: list[Finding] = []
    boms = config.boms
    for rule in boms.standalone_fba_prep:
        if rule.consumes not in boms.components:
            findings.append(
                error(
                    f"An FBA prep rule uses part '{rule.consumes.part}' from "
                    f"'{rule.consumes.supplier}', which is not a component in this file.",
                    rule.loc,
                    blocks_run=False,
                )
            )
        if rule.sku is None and not rule.applies_to_all_fba_units:
            findings.append(
                warning(
                    "An FBA prep rule has no SKU yet, so the packaging it consumes is not "
                    "being counted.",
                    rule.loc,
                    fix="Fill in the SKU it applies to.",
                )
            )
    return findings


def _check_recipients(config: LoadedConfig) -> list[Finding]:
    findings: list[Finding] = []
    shannon = config.shannon
    for group in shannon.ops_reminder_groups:
        for role in group.recipients:
            recipient = shannon.recipients.get(role)
            if recipient is None:
                findings.append(
                    error(
                        f"The '{group.name}' reminder is addressed to '{role}', but no "
                        "such person is listed under 'recipients:'. The reminder would go "
                        "nowhere.",
                        group.loc,
                        fix=f"Add '{role}:' with an email address under 'recipients:'.",
                    )
                )
            elif recipient.email is None and recipient.sms is None:
                findings.append(
                    error(
                        f"'{role}' has neither an email address nor a phone number, so "
                        f"the '{group.name}' reminder would go nowhere.",
                        recipient.loc,
                    )
                )
        for key in group.components:
            component = config.boms.components.get(key)
            if component is None:
                findings.append(
                    error(
                        f"The '{group.name}' reminder lists part '{key.part}' from "
                        f"'{key.supplier}', which is not a component in the BOM file.",
                        group.loc,
                    )
                )
            elif component.component_class is not ComponentClass.OPS_CONSUMABLE:
                findings.append(
                    error(
                        f"The '{group.name}' reminder lists {key}, which is a "
                        f"'{component.component_class.value}' item. Reminders are only for "
                        "'ops_consumable' items.",
                        group.loc,
                    )
                )
    if not shannon.from_address:
        findings.append(
            error(
                "Shannon has no 'from_address', so nothing she writes could ever be sent.",
                Loc(file=config.entity.shannon_config, line=1),
                fix="Add 'identity:' with a 'from_address:' for this business.",
            )
        )
    return findings


__all__ = ["ASIN_PATTERN", "ValidationResult", "validate"]
