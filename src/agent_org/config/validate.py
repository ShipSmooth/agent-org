"""The checks behind `shannon validate-config` (docs/replenishment.md §13).

This command runs before every execution. Zach maintains the parts list by
hand; this is what makes that safe. Every message names a file and a line
and says what to do about it.

The committed BOM is expected to pass. Unresolved channel SKUs are the one
known gap (parking-lot item PL-8): they are reported as warnings every run,
because they under-count that kit's sales, but they do not stop a run that
is useful for the other kits. The error paths are proved against
tests/fixtures/invalid, a configuration broken on purpose.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from agent_org.config.errors import Finding, Severity, error, warning
from agent_org.config.models import (
    Capability,
    Component,
    ComponentClass,
    ComponentKey,
    LoadedConfig,
    cover_target_for,
)
from agent_org.config.yamlsource import UNKNOWN_LOC, Loc

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


def validate(
    config: LoadedConfig,
    extra: Sequence[Finding] | None = None,
    today: date | None = None,
) -> ValidationResult:
    findings: list[Finding] = list(extra or [])
    boms = config.boms
    entity = config.entity
    channel_keys = set(entity.channel_keys)

    findings += _check_suppliers(config)
    findings += _check_components(config, today or date.today())
    findings += _check_listings(config, channel_keys)
    findings += _check_kits(config, channel_keys)
    findings += _check_fba_prep(config)
    findings += _check_recipients(config)
    findings += _check_veeqo_channels(config)

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


def _check_components(config: LoadedConfig, today: date) -> list[Finding]:
    findings: list[Finding] = []
    boms = config.boms
    # FBA packaging is consumed by a prep rule rather than by a kit line, so
    # it counts as used: it is not a part somebody forgot to attach.
    used_by_kits = {line.component for kit in boms.kits.values() for line in kit.lines} | {
        rule.consumes for rule in boms.standalone_fba_prep
    }
    for key, component in boms.components.items():
        findings += _check_kit_membership(component, key in used_by_kits)
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
            ("sales_asin", component.sales_asin),
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
        findings += _check_stock_source(key, component, today)
        if component.component_class is ComponentClass.REORDER_POINT:
            findings += _check_reorder_thresholds(component.key, component)
        if component.component_class in PURCHASABLE_CLASSES:
            findings += _check_lead_time(config, key)
        if component.part_is_internal_reference and not component.name.strip():
            findings.append(
                error(
                    f"Component {key} is marked 'part_is_internal_reference' but has no "
                    "name. The part number is ours, not the supplier's, so the name is "
                    "the only thing that can be put on a purchase order.",
                    component.loc,
                    fix="Add 'name:' with the product name exactly as the supplier sells it.",
                )
            )
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


def _check_kit_membership(component: Component, used: bool) -> list[Finding]:
    """Whether a component belonging to no kit is a gap or the normal state.

    A NAR finished kit is bought complete and resold as it comes: it is
    forecast from its own sales and never appears inside anything, so
    `resale_only` says so and no warning is raised. A part that is *not*
    marked that way and is in no kit is different — most likely a kit line
    was deleted or its part number was mistyped, and the line will be
    ordered for a kit that no longer asks for it.
    """
    if component.component_class not in PURCHASABLE_CLASSES:
        return []
    if used:
        if component.resale_only:
            return [
                error(
                    f"Component {component.key} is marked 'resale_only' but a kit uses "
                    "it as a part. It cannot be both bought complete for resale and "
                    "consumed by an assembly.",
                    component.loc,
                    fix=(
                        "Remove 'resale_only: true', or remove the kit line that uses "
                        "it. If the resold product and the kit part are genuinely two "
                        "different things, give them separate part numbers."
                    ),
                    blocks_run=False,
                )
            ]
        return []
    if component.resale_only:
        return []
    return [
        warning(
            f"Component {component.key} ({component.name}) is in no kit, and it is not "
            "marked 'resale_only', so Shannon will forecast it from its own sales "
            "alone. If it should be part of a kit, that kit line is missing.",
            component.loc,
            fix=(
                "Add it to the kit that uses it, or mark it 'resale_only: true' if it "
                "is a finished product bought complete and resold as it comes."
            ),
        )
    ]


def _check_listings(config: LoadedConfig, channel_keys: set[str]) -> list[Finding]:
    """Amazon identity is data, so the only checks are that it lines up.

    A kit or component with no listing at all is not a finding: three kits
    have never been on Amazon and their Amazon velocity is structurally
    zero, which is a fact rather than a gap.
    """
    findings: list[Finding] = []
    listings = config.listings
    boms = config.boms
    claimed: dict[str, str] = {}
    parts = {key.part for key in boms.components}
    unknown: list[str] = []
    subjects = list(listings.kits.items()) + list(listings.components.items())
    for subject, listing_set in subjects:
        if subject not in boms.kits and subject not in parts:
            # Said once, at the end: Zach lists far more than he assembles, and
            # forty near-identical warnings would bury the ones that matter.
            unknown.append(subject)
        for listing in listing_set.listings:
            if listing.channel and listing.channel not in channel_keys:
                findings.append(
                    error(
                        f"'{subject}' has a listing on a channel called "
                        f"'{listing.channel}', which is not a channel this business "
                        "sells on.",
                        listing.loc,
                        fix="Use one of: " + ", ".join(sorted(channel_keys)) + ".",
                    )
                )
            if listing.sku in claimed and claimed[listing.sku] != subject:
                findings.append(
                    error(
                        f"Channel SKU '{listing.sku}' is claimed by both "
                        f"'{claimed[listing.sku]}' and '{subject}'. Sales of it would be "
                        "counted twice.",
                        listing.loc,
                    )
                )
            claimed[listing.sku] = subject
        if listing_set.demand_is_suppressed:
            findings.append(
                warning(
                    f"Every listing for '{subject}' is inactive, so its recent sales "
                    "understate demand rather than measure it. Shannon reports it as "
                    "suppressed and puts it in the parking lot for you.",
                    listing_set.loc,
                    fix="Restock and relist, or decide to discontinue it.",
                )
            )
    if unknown:
        findings.append(
            warning(
                f"{len(unknown)} listing(s) are for things the BOM does not describe, so "
                "Shannon can read their sales but can never reorder them: "
                + ", ".join(sorted(unknown))
                + ".",
                UNKNOWN_LOC if not subjects else subjects[0][1].loc,
                fix="Add them to the BOM as components, or leave them if they are retired.",
            )
        )
    return findings


def _check_stock_source(key: ComponentKey, component: Component, today: date) -> list[Finding]:
    """A hand-counted component must actually carry a hand count.

    Without this, `stock_source: manual` means "do not ask Veeqo" and nothing
    else, so the component reads zero every week and Shannon proposes buying
    it every week forever. That is the exact failure this field exists to
    prevent, so it is an error and it blocks the run.
    """
    findings: list[Finding] = []
    if not component.counted_by_hand:
        if component.manual_stock is not None:
            findings.append(
                warning(
                    f"Component {key} carries a hand count but takes its stock from Veeqo, "
                    "so the count is never read.",
                    component.manual_stock.loc,
                    fix="Add 'stock_source: manual', or delete the 'manual_stock' block.",
                )
            )
        return findings
    if component.manual_stock is None:
        findings.append(
            error(
                f"Component {key} says its stock is counted by hand but gives no count, "
                "so Shannon would read it as zero and propose buying it every week.",
                component.loc,
                fix=(
                    "Add 'manual_stock:' with 'count:' and 'counted_on:', or set "
                    "'stock_source: veeqo'."
                ),
            )
        )
        return findings
    if component.manual_stock.counted_on > today:
        findings.append(
            error(
                f"Component {key} was counted on "
                f"{component.manual_stock.counted_on.isoformat()}, which has not happened "
                "yet.",
                component.manual_stock.loc,
                fix="Correct 'counted_on' to the day the shelf was actually counted.",
            )
        )
    return findings


def _check_reorder_thresholds(key: ComponentKey, component: Component) -> list[Finding]:
    findings: list[Finding] = []
    if component.reorder_target is not None and component.reorder_quantity is not None:
        findings.append(
            error(
                f"Component {key} sets both 'reorder_target' "
                f"({component.reorder_target}) and 'reorder_quantity' "
                f"({component.reorder_quantity}). Those are two different instructions \u2014 "
                "top up to a level, or buy a fixed amount \u2014 and Shannon will not pick "
                "one for you.",
                component.loc,
                fix="Delete whichever one is not what Zach meant.",
            )
        )
        return findings
    if component.reorder_point is None or (
        component.reorder_target is None and component.reorder_quantity is None
    ):
        findings.append(
            warning(
                f"Component {key} is a reorder-point item but its reorder point, or what "
                "to buy when it is hit, is still unresolved, so Shannon cannot say when "
                "it runs low.",
                component.loc,
                fix=(
                    "Fill in 'reorder_point', and either 'reorder_quantity' (buy this "
                    "many) or 'reorder_target' (top up to this level)."
                ),
            )
        )
        return findings
    if component.reorder_quantity is not None:
        if component.reorder_quantity < 1:
            findings.append(
                error(
                    f"Component {key} has a reorder quantity of "
                    f"{component.reorder_quantity}, which would order nothing.",
                    component.loc,
                    fix="Write the number of sellable units to buy, for example 1000.",
                )
            )
        return findings
    if component.reorder_target is None:
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
    cover = cover_target_for(component, supplier, config.shannon.parameters.cover_target_weeks)
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
                listing_set = config.listings.for_kit(kit_group)
                if listing_set is not None and listing_set.covers(channel_key):
                    # listings.yaml is the authority on Amazon identity: either it
                    # gives the channel SKU, or it says there is no listing on that
                    # channel, which is a fact rather than a gap (PL-8). It says
                    # nothing about Shopify, so a gap there is still a gap.
                    continue
                findings.append(
                    warning(
                        f"Kit '{kit_group}' has no SKU for the '{channel_key}' channel — "
                        "the file says TODO, so sales on that channel are not counted "
                        "and this kit will be under-ordered until the real SKU is in.",
                        kit.loc,
                        fix=(
                            "Put the SKU that channel holds in 'aliases'. Amazon's own "
                            "SKUs live in listings.yaml instead, so this is only ever "
                            "asked about a channel that file does not speak for."
                        ),
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


def _check_veeqo_channels(config: LoadedConfig) -> list[Finding]:
    """That each channel maps to exactly one Veeqo name, or to none on purpose.

    Velocity is split on these strings. Two channels claiming the same name
    would send one channel's sales to the other, and a name that is both
    mapped and excluded would make counting depend on which check ran first.
    Both are configuration mistakes with quiet consequences, so they are
    errors here rather than surprises in a report.
    """
    findings: list[Finding] = []
    entity = config.entity
    loc = UNKNOWN_LOC
    seen: dict[str, str] = {}
    for channel in entity.channels:
        name = (channel.veeqo_channel or "").strip()
        if not channel.in_veeqo:
            if channel.has_history:
                findings.append(
                    error(
                        f"Channel '{channel.key}' is marked 'not_connected' in Veeqo but "
                        "also has_history: true. A channel with sales has to be readable, "
                        "or its demand goes missing.",
                        loc,
                        fix=(
                            "Either give the channel the name Veeqo prints on its orders, "
                            "or set has_history: false."
                        ),
                    )
                )
            continue
        if not name:
            continue
        first = seen.get(name)
        if first is not None:
            findings.append(
                error(
                    f"Channels '{first}' and '{channel.key}' both claim the Veeqo channel "
                    f"'{name}'. Sales on it would be counted against one of them, and "
                    "which one is not something Shannon should decide.",
                    loc,
                    fix="Give each channel the name Veeqo actually prints on its orders.",
                )
            )
        seen[name] = channel.key
    for excluded in entity.excluded_veeqo_channels:
        owner = seen.get(excluded)
        if owner is not None:
            findings.append(
                error(
                    f"Veeqo channel '{excluded}' is listed under "
                    f"'excluded_veeqo_channels' and is also the channel for "
                    f"'{owner}'. Its sales cannot be both counted and not counted.",
                    loc,
                    fix="Remove it from one of the two lists.",
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
    findings += _check_report_recipients(config)
    return findings


def _check_report_recipients(config: LoadedConfig) -> list[Finding]:
    """Who the weekly report is emailed to, and that it stays inside the business.

    Zach holds one email identity per company, and they are not
    interchangeable: the vendor/tooling identity receiving iThrive's
    operational mail mixes correspondence between two LLCs. The rule is
    stated generally rather than as a list of banned addresses — mail goes
    to the operating domain Shannon sends from, and anything else is named
    and refused here rather than discovered in a sent-items folder.
    """
    findings: list[Finding] = []
    shannon = config.shannon
    where = Loc(file=config.entity.shannon_config, line=1)
    if not shannon.report_email_roles:
        findings.append(
            warning(
                "No role is listed under 'reports: email_to:', so the weekly report is "
                "written to disk and to the database but emailed to nobody.",
                where,
                fix="Add 'reports:' with 'email_to: [zach]'.",
            )
        )
    _, _, home_domain = shannon.from_address.partition("@")
    for role in shannon.report_email_roles:
        recipient = shannon.recipients.get(role)
        if recipient is None:
            findings.append(
                error(
                    f"The weekly report is addressed to '{role}', but no such person is "
                    "listed under 'recipients:', so the report would be emailed nowhere.",
                    where,
                    fix=f"Add '{role}:' with an email address under 'recipients:'.",
                )
            )
            continue
        if not recipient.email:
            findings.append(
                error(
                    f"'{role}' receives the weekly report but has no email address.",
                    recipient.loc,
                    fix="Add 'email:' for this person, or take the role out of "
                    "'reports: email_to:'.",
                )
            )
            continue
        _, _, domain = recipient.email.partition("@")
        if home_domain and domain.lower() != home_domain.lower():
            findings.append(
                error(
                    f"'{role}' would receive {config.entity.legal_name}'s weekly report at "
                    f"{recipient.email}, which is not on {home_domain} — the domain Shannon "
                    "sends this entity's mail from. Each business has its own address, and "
                    "operational mail crossing between them mixes the correspondence of two "
                    "separate companies.",
                    recipient.loc,
                    fix=f"Use this person's {home_domain} address, or take the role out of "
                    "'reports: email_to:'.",
                )
            )
    return findings


__all__ = ["ASIN_PATTERN", "ValidationResult", "validate"]
