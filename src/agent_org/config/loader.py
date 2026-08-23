"""Turn the YAML on disk into typed configuration objects.

The loader is deliberately lenient about *unresolved* values (`TODO`,
`null` pack sizes) and strict about *malformed* ones. Unresolved values
become `None` and are reported by `agent_org.config.validate`; malformed
structure raises `ConfigError` immediately, because there is nothing
sensible to report against.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from agent_org.config.errors import ConfigError, Finding, error
from agent_org.config.models import (
    AgentSchedule,
    BomConfig,
    BomLine,
    Capability,
    Channel,
    Component,
    ComponentClass,
    ComponentKey,
    EntityConfig,
    Kit,
    LoadedConfig,
    OpsReminderGroup,
    PackSizePolicy,
    Parameters,
    ParkingLotItem,
    PolicyConfig,
    PolicyRule,
    Recipient,
    ShannonConfig,
    StandaloneFbaPrep,
    Supplier,
)
from agent_org.config.yamlsource import UNKNOWN_LOC, Loc, YamlMap, load_yaml_file, loc_of

# Values that mean "a human still has to answer this". They load as None and
# are reported, never guessed at and never treated as a real identifier.
UNRESOLVED = frozenset({"TODO", "TBD", "todo", "tbd", None})


def is_unresolved(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in UNRESOLVED)


@dataclass(frozen=True)
class ConfigPaths:
    root: Path

    def entity_file(self, entity_id: str) -> Path:
        return self.root / "entities" / f"{entity_id}.yaml"

    def resolve(self, relative: str) -> Path:
        return (self.root.parent / relative).resolve()

    def policy_global(self) -> Path:
        return self.root / "policy" / "global.yaml"


def _require_map(value: Any, what: str, loc: Loc) -> YamlMap:
    if not isinstance(value, YamlMap):
        raise ConfigError([error(f"{what} must be a block of settings, not a single value.", loc)])
    return value


def _require_list(value: Any, what: str, loc: Loc) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError([error(f"{what} must be a list.", loc)])
    return value


def _int_or_none(value: Any) -> int | None:
    if is_unresolved(value) or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _fraction_or_none(value: Any) -> Fraction | None:
    if is_unresolved(value) or isinstance(value, bool):
        return None
    if isinstance(value, int | float | str):
        try:
            return Fraction(str(value).strip())
        except (ValueError, ZeroDivisionError):
            return None
    return None


def _str_or_none(value: Any) -> str | None:
    if is_unresolved(value):
        return None
    return str(value)


def load_entity(paths: ConfigPaths, entity_id: str) -> EntityConfig:
    path = paths.entity_file(entity_id)
    if not path.exists():
        raise ConfigError(
            [
                error(
                    f"There is no entity called '{entity_id}'.",
                    Loc(file=str(path), line=0),
                    fix=(
                        "Create the file above, copying an existing entity file, "
                        "or check the spelling of the entity name."
                    ),
                )
            ]
        )
    raw = _require_map(load_yaml_file(path), "The entity file", Loc(str(path), 1))
    channels = tuple(
        Channel(
            name=str(item["name"]),
            key=str(item.get("key", item["name"])),
            fulfillment=str(item.get("fulfillment", "merchant")),
            has_history=bool(item.get("has_history", True)),
        )
        for item in _require_list(raw.get("channels", []), "channels", raw.loc_of("channels"))
    )
    agents = tuple(
        AgentSchedule(
            name=str(item["name"]),
            kind=str(item.get("kind", "")),
            schedule=str(item.get("schedule", "")),
        )
        for item in _require_list(raw.get("agents", []), "agents", raw.loc_of("agents"))
    )
    return EntityConfig(
        entity_id=str(raw.get("entity_id", entity_id)),
        legal_name=str(raw.get("legal_name", entity_id)),
        status=str(raw.get("status", "active")),
        timezone=str(raw.get("timezone", "UTC")),
        channels=channels,
        agents=agents,
        credentials_prefix=str(raw.get("credentials_prefix", "")),
        suppliers_config=str(raw.get("suppliers_config", "")),
        boms_config=str(raw.get("boms_config", "")),
        shannon_config=str(raw.get("shannon_config", "")),
        policy_config=str(raw.get("policy_config", "")),
    )


def _load_supplier_capabilities(path: Path) -> dict[str, YamlMap]:
    raw = _require_map(load_yaml_file(path), "The supplier file", Loc(str(path), 1))
    suppliers = raw.get("suppliers")
    if not isinstance(suppliers, YamlMap):
        raise ConfigError(
            [error("The supplier file needs a 'suppliers:' block.", raw.loc_of("suppliers"))]
        )
    out: dict[str, YamlMap] = {}
    for key, value in suppliers.items():
        out[key] = _require_map(value, f"Supplier '{key}'", suppliers.loc_of(key))
    return out


def load_boms(boms_path: Path, suppliers_path: Path) -> tuple[BomConfig, list[Finding]]:
    findings: list[Finding] = []
    raw = _require_map(load_yaml_file(boms_path), "The BOM file", Loc(str(boms_path), 1))
    capability_blocks = _load_supplier_capabilities(suppliers_path)

    suppliers: dict[str, Supplier] = {}
    supplier_block = _require_map(
        raw.get("suppliers", YamlMap()), "suppliers", raw.loc_of("suppliers")
    )
    for key, value in supplier_block.items():
        entry = _require_map(value, f"Supplier '{key}'", supplier_block.loc_of(key))
        caps_block = capability_blocks.get(key)
        capabilities: set[Capability] = set()
        if caps_block is not None:
            for name in _require_list(
                caps_block.get("capabilities", []), "capabilities", caps_block.loc
            ):
                try:
                    capabilities.add(Capability(str(name)))
                except ValueError:
                    findings.append(
                        error(
                            f"Supplier '{key}' lists an unknown capability '{name}'.",
                            caps_block.loc_of("capabilities"),
                            fix=(
                                "Use one of: "
                                + ", ".join(sorted(c.value for c in Capability))
                                + "."
                            ),
                        )
                    )
        lead_time = _fraction_or_none(entry.get("lead_time_weeks"))
        if lead_time is None and caps_block is not None:
            lead_time = _fraction_or_none(caps_block.get("lead_time_weeks"))
        suppliers[key] = Supplier(
            key=key,
            name=str(entry.get("name", key)),
            acquisition=str(entry.get("acquisition", "none")),
            capabilities=frozenset(capabilities),
            lead_time_weeks=lead_time,
            loc=supplier_block.loc_of(key),
        )

    pack_block = _require_map(
        raw.get("pack_size_policy", YamlMap()), "pack_size_policy", raw.loc_of("pack_size_policy")
    )
    pack_policy = PackSizePolicy(
        mode=str(pack_block.get("mode", "discover_and_confirm")),
        on_mismatch=str(pack_block.get("on_mismatch", "halt_line_and_flag")),
    )

    components: dict[ComponentKey, Component] = {}
    for item in _require_list(raw.get("components", []), "components", raw.loc_of("components")):
        entry = _require_map(item, "A component", loc_of(item))
        component_key = ComponentKey(
            supplier=str(entry.get("supplier")), part=str(entry.get("part"))
        )
        component_class = ComponentClass.FORECAST
        if "class" not in entry:
            findings.append(
                error(
                    f"Component {component_key} has no 'class'. Shannon will not guess whether "
                    "something can be bought.",
                    entry.loc,
                    fix=(
                        "Add 'class:' with one of: "
                        + ", ".join(sorted(c.value for c in ComponentClass))
                        + "."
                    ),
                )
            )
        else:
            try:
                component_class = ComponentClass(str(entry.get("class")))
            except ValueError:
                findings.append(
                    error(
                        f"Component {component_key} has class "
                        f"'{entry.get('class')}', which is not a class Shannon knows.",
                        entry.loc_of("class"),
                        fix=(
                            "Use one of: "
                            + ", ".join(sorted(c.value for c in ComponentClass))
                            + "."
                        ),
                    )
                )
        if "units_per_purchase_unit" not in entry:
            findings.append(
                error(
                    f"Component {component_key} does not say how many sellable units are in one "
                    "purchase unit.",
                    entry.loc,
                    fix=(
                        "Add 'units_per_purchase_unit: 1' for singles, the pack size for "
                        "packs, or 'null' to let Shannon read it off the listing."
                    ),
                )
            )
        if component_key in components:
            findings.append(
                error(
                    f"Component {component_key} is listed twice.",
                    entry.loc,
                    fix="Delete one of the two entries.",
                )
            )
        components[component_key] = Component(
            key=component_key,
            name=str(entry.get("name", component_key.part)),
            component_class=component_class,
            units_per_purchase_unit=_int_or_none(entry.get("units_per_purchase_unit")),
            purchase_unit_name=_str_or_none(entry.get("purchase_unit_name")),
            moq_min=_int_or_none(entry.get("moq_min")) or 0,
            moq_increment=_int_or_none(entry.get("moq_increment")) or 0,
            reorder_point=_int_or_none(entry.get("reorder_point")),
            reorder_target=_int_or_none(entry.get("reorder_target")),
            purchase_asin=_str_or_none(entry.get("purchase_asin"))
            or (component_key.part if component_key.supplier == "amazon_business" else None),
            cover_target_weeks=_fraction_or_none(entry.get("cover_target_weeks")),
            safety_stock_weeks=_fraction_or_none(entry.get("safety_stock_weeks")),
            loc=entry.loc,
        )

    kits: dict[str, Kit] = {}
    kit_block = _require_map(raw.get("kits", YamlMap()), "kits", raw.loc_of("kits"))
    for kit_group, value in kit_block.items():
        entry = _require_map(value, f"Kit '{kit_group}'", kit_block.loc_of(kit_group))
        aliases_block = _require_map(
            entry.get("aliases", YamlMap()), f"Kit '{kit_group}' aliases", entry.loc_of("aliases")
        )
        aliases: dict[str, str | None] = {
            channel: _str_or_none(sku) for channel, sku in aliases_block.items()
        }
        lines: list[BomLine] = []
        for line_item in _require_list(
            entry.get("components", []), f"Kit '{kit_group}' components", entry.loc_of("components")
        ):
            lines.append(_parse_bom_line(line_item))
        pouch = entry.get("pouch")
        if pouch is not None:
            lines.append(_parse_bom_line(pouch))
        kits[kit_group] = Kit(
            kit_group=kit_group,
            name=str(entry.get("name", kit_group)),
            aliases=aliases,
            lines=tuple(lines),
            build_blocked=_str_or_none(entry.get("build_blocked")),
            loc=entry.loc,
        )

    prep: list[StandaloneFbaPrep] = []
    for item in _require_list(
        raw.get("standalone_fba_prep", []), "standalone_fba_prep", raw.loc_of("standalone_fba_prep")
    ):
        entry = _require_map(item, "An FBA prep rule", loc_of(item))
        consumes = _require_map(entry.get("consumes", YamlMap()), "consumes", entry.loc)
        prep.append(
            StandaloneFbaPrep(
                sku=_str_or_none(entry.get("sku")),
                applies_to_all_fba_units=str(entry.get("applies_to", "")) == "all_fba_units",
                consumes=ComponentKey(
                    supplier=str(consumes.get("supplier")), part=str(consumes.get("part"))
                ),
                qty=_int_or_none(consumes.get("qty")) or 1,
                loc=entry.loc,
            )
        )

    parking_lot = tuple(
        ParkingLotItem(
            id=str(_require_map(item, "A parking lot item", loc_of(item)).get("id", "PL-?")),
            item=str(item.get("item", "")),
            detail=_str_or_none(item.get("detail")),
            blocks=_str_or_none(item.get("blocks")),
            loc=loc_of(item),
        )
        for item in _require_list(
            raw.get("parking_lot", []), "parking_lot", raw.loc_of("parking_lot")
        )
    )

    bom_version = str(raw.get("bom_version", "unversioned"))
    return (
        BomConfig(
            bom_version=bom_version,
            pack_size_policy=pack_policy,
            suppliers=suppliers,
            components=components,
            kits=kits,
            standalone_fba_prep=tuple(prep),
            parking_lot=parking_lot,
        ),
        findings,
    )


def _parse_bom_line(item: Any) -> BomLine:
    entry = _require_map(item, "A BOM line", loc_of(item))
    channels_value = entry.get("channels")
    channels: frozenset[str] | None = None
    if channels_value is not None:
        channels = frozenset(str(channel) for channel in channels_value)
    return BomLine(
        component=ComponentKey(supplier=str(entry.get("supplier")), part=str(entry.get("part"))),
        qty=_int_or_none(entry.get("qty")) or 0,
        channels=channels,
        loc=entry.loc,
    )


def load_shannon(path: Path) -> ShannonConfig:
    raw = _require_map(load_yaml_file(path), "The Shannon config file", Loc(str(path), 1))
    identity = _require_map(raw.get("identity", YamlMap()), "identity", raw.loc_of("identity"))
    recipients_block = _require_map(
        raw.get("recipients", YamlMap()), "recipients", raw.loc_of("recipients")
    )
    recipients = {
        role: Recipient(
            role=role,
            email=_str_or_none(
                _require_map(value, f"Recipient '{role}'", recipients_block.loc_of(role)).get(
                    "email"
                )
            ),
            sms=_str_or_none(value.get("sms")),
            loc=recipients_block.loc_of(role),
        )
        for role, value in recipients_block.items()
    }
    params_block = _require_map(
        raw.get("parameters", YamlMap()), "parameters", raw.loc_of("parameters")
    )
    missing = [
        name
        for name in (
            "velocity_window_days",
            "cover_target_weeks",
            "safety_stock_weeks",
            "round_up_to_nearest",
            "mf_floor_weeks",
            "fba_cover_weeks",
        )
        if name not in params_block
    ]
    if missing:
        raise ConfigError(
            [
                error(
                    "These replenishment parameters are missing: " + ", ".join(missing) + ".",
                    params_block.loc,
                    fix="Add each one under 'parameters:'. See docs/replenishment.md section 9.",
                )
            ]
        )

    def frac(name: str, default: str) -> Fraction:
        return _fraction_or_none(params_block.get(name)) or Fraction(default)

    def integer(name: str, default: int) -> int:
        value = _int_or_none(params_block.get(name))
        return default if value is None else value

    parameters = Parameters(
        velocity_window_days=integer("velocity_window_days", 90),
        cover_target_weeks=frac("cover_target_weeks", "7"),
        safety_stock_weeks=_fraction_or_none(params_block.get("safety_stock_weeks")) or Fraction(0),
        round_up_to_nearest=integer("round_up_to_nearest", 5),
        mf_floor_weeks=frac("mf_floor_weeks", "2"),
        fba_cover_weeks=frac("fba_cover_weeks", "8"),
        walmart_reserve_units=integer("walmart_reserve_units", 0),
        box_min=integer("box_min", 5),
        box_max=integer("box_max", 10),
        overship_tolerance=integer("overship_tolerance", 0),
        task_wall_clock_budget_seconds=integer("task_wall_clock_budget_seconds", 1800),
        task_step_budget=integer("task_step_budget", 200),
    )

    reminders = _require_map(
        raw.get("ops_reminders", YamlMap()), "ops_reminders", raw.loc_of("ops_reminders")
    )
    groups_block = _require_map(
        reminders.get("groups", YamlMap()), "ops_reminders groups", reminders.loc_of("groups")
    )
    groups = tuple(
        OpsReminderGroup(
            name=name,
            recipients=tuple(
                str(role)
                for role in _require_map(
                    value, f"Reminder group '{name}'", groups_block.loc_of(name)
                ).get("recipients", [])
            ),
            components=tuple(
                ComponentKey(supplier=str(entry.get("supplier")), part=str(entry.get("part")))
                for entry in value.get("components", [])
            ),
            loc=groups_block.loc_of(name),
        )
        for name, value in groups_block.items()
    )

    return ShannonConfig(
        from_name=str(identity.get("from_name", "Shannon")),
        from_address=str(identity.get("from_address", "")),
        recipients=recipients,
        parameters=parameters,
        ops_reminder_cadence_weeks=_int_or_none(reminders.get("cadence_weeks")) or 6,
        ops_reminder_groups=groups,
    )


def load_policy(global_path: Path, entity_path: Path | None) -> PolicyConfig:
    raw = _require_map(load_yaml_file(global_path), "The policy file", Loc(str(global_path), 1))
    rules: dict[str, PolicyRule] = {}
    for item in _require_list(raw.get("rules", []), "rules", raw.loc_of("rules")):
        entry = _require_map(item, "A policy rule", loc_of(item))
        action = str(entry.get("action"))
        tier = _int_or_none(entry.get("tier"))
        if tier is None:
            raise ConfigError([error(f"Policy rule '{action}' has no tier.", entry.loc)])
        rules[action] = PolicyRule(action=action, tier=tier)

    thresholds: dict[str, object] = dict(raw.get("thresholds", {}))
    default_tier = _int_or_none(raw.get("default_tier"))
    max_tier = _int_or_none(raw.get("max_tier_this_phase"))
    if default_tier is None or max_tier is None:
        raise ConfigError(
            [
                error(
                    "The policy file must set both 'default_tier' and 'max_tier_this_phase'.",
                    raw.loc,
                )
            ]
        )

    if entity_path is not None and entity_path.exists():
        entity_raw = _require_map(
            load_yaml_file(entity_path), "The entity policy file", Loc(str(entity_path), 1)
        )
        for item in entity_raw.get("rules", []):
            entry = _require_map(item, "A policy rule", loc_of(item))
            action = str(entry.get("action"))
            tier = _int_or_none(entry.get("tier")) or default_tier
            existing = rules.get(action)
            if existing is not None and tier < existing.tier:
                raise ConfigError(
                    [
                        error(
                            f"The entity policy lowers '{action}' from tier {existing.tier} to "
                            f"tier {tier}. An entity may raise a tier, never lower one.",
                            entry.loc,
                            fix="Remove the override, or change the global rule instead.",
                        )
                    ]
                )
            rules[action] = PolicyRule(action=action, tier=tier)
        entity_thresholds = entity_raw.get("thresholds")
        if isinstance(entity_thresholds, dict):
            thresholds.update(entity_thresholds)

    return PolicyConfig(
        version=_int_or_none(raw.get("version")) or 1,
        default_tier=default_tier,
        max_tier_this_phase=max_tier,
        rules=rules,
        thresholds=thresholds,
        escalations=tuple(dict(item) for item in raw.get("escalations", [])),
    )


def load_config(config_root: Path, entity_id: str) -> tuple[LoadedConfig, list[Finding]]:
    """Load everything for one entity. Never reads another entity's files."""
    paths = ConfigPaths(root=config_root)
    entity = load_entity(paths, entity_id)
    boms, findings = load_boms(
        paths.resolve(entity.boms_config), paths.resolve(entity.suppliers_config)
    )
    shannon = load_shannon(paths.resolve(entity.shannon_config))
    policy = load_policy(
        paths.policy_global(),
        paths.resolve(entity.policy_config) if entity.policy_config else None,
    )
    return (
        LoadedConfig(entity=entity, boms=boms, shannon=shannon, policy=policy),
        findings,
    )


__all__ = [
    "UNKNOWN_LOC",
    "ConfigPaths",
    "is_unresolved",
    "load_boms",
    "load_config",
    "load_entity",
    "load_policy",
    "load_shannon",
]
