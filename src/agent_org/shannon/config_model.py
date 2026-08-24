"""Typed view of Shannon's config files.

Loads ``config/entities/<entity>.yaml``, ``config/<entity>/boms.yaml``,
``config/<entity>/shannon.yaml`` and (fixtures only, optional)
``config/<entity>/products.yaml`` into dataclasses, keeping the file and
line of every record so validation and run-time errors can name both.

Loading is deliberately tolerant: it records issues instead of raising,
so ``shannon validate-config`` can report *every* problem in one pass.
Callers that need a clean config check ``issues_fatal`` first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from agent_org.shannon.configload import (
    Issue,
    Loc,
    Map,
    Reader,
    Scalar,
    Seq,
    is_todo,
    load_file,
    scalar_text,
)

ASIN_SUPPLIER = "amazon_business"


@dataclass(frozen=True)
class ComponentKey:
    supplier: str
    part: str

    def __str__(self) -> str:
        return f"{self.supplier}/{self.part}"


@dataclass
class SupplierCfg:
    key: str
    name: str
    acquisition: str
    lead_time_weeks: Fraction | None
    lead_time_todo: bool
    loc: Loc


@dataclass
class ComponentCfg:
    key: ComponentKey
    name: str
    cls: str | None  # forecast | reorder_point | non_stocked | ops_consumable
    units_per_purchase_unit: int | None
    upu_missing: bool  # key absent entirely (explicit null is not missing)
    purchase_unit_name: str | None
    purchase_asin: str | None
    moq_min: int
    moq_increment: int
    reorder_point: int | None
    reorder_target: int | None
    thresholds_todo: bool
    loc: Loc


@dataclass
class BomLine:
    key: ComponentKey
    qty: int
    channels: list[str] | None
    loc: Loc


@dataclass
class KitCfg:
    kit_group: str
    name: str
    aliases: dict[str, str]  # channel key -> SKU ('TODO' preserved)
    alias_locs: dict[str, Loc]
    components: list[BomLine]
    pouch: BomLine | None
    build_blocked: str | None
    loc: Loc


@dataclass
class StandalonePrep:
    sku: str | None  # None for the applies_to: all_fba_units row
    applies_to_all: bool
    key: ComponentKey
    qty: int
    loc: Loc


@dataclass
class ParkingItem:
    pl_id: str
    item: str
    detail: str | None
    blocks: str | None
    loc: Loc


@dataclass
class StandaloneProduct:
    sku: str
    key: ComponentKey
    sales_asin: str | None
    loc: Loc


@dataclass
class ChannelCfg:
    key: str  # 'fba', 'fbm', 'shopify', 'walmart_sf', 'walmart_wfs'
    name: str  # 'amazon_fba', ...
    fulfillment: str  # 'fba' | 'merchant' | 'wfs'
    has_history: bool
    loc: Loc


@dataclass
class Params:
    velocity_window_days: int = 90
    cover_target_weeks: Fraction = Fraction(7)
    safety_stock_weeks: Fraction = Fraction(0)
    round_up_to_nearest: int = 5
    mf_floor_weeks: Fraction = Fraction(2)
    fba_cover_weeks: Fraction = Fraction(8)
    walmart_reserve_units: int = 0
    box_min: int = 5
    box_max: int = 10
    overship_tolerance: int = 0


@dataclass
class OpsGroup:
    name: str
    recipients: list[str]
    components: list[ComponentKey]
    loc: Loc


@dataclass
class ShannonCfg:
    from_name: str
    from_address: str
    recipients: dict[str, dict[str, str]]  # role -> {email, sms}
    params: Params
    ops_cadence_weeks: int
    ops_groups: list[OpsGroup]
    loc: Loc


@dataclass
class EntityConfig:
    entity_id: str
    legal_name: str
    timezone: str
    channels: list[ChannelCfg]
    bom_version: str
    pack_size_mode: str
    suppliers: dict[str, SupplierCfg]
    components: dict[ComponentKey, ComponentCfg]
    kits: dict[str, KitCfg]
    standalone_prep: list[StandalonePrep]
    parking_lot: list[ParkingItem]
    standalone_products: list[StandaloneProduct]
    shannon: ShannonCfg
    issues: list[Issue] = field(default_factory=list)
    config_texts: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    def channel_keys(self) -> set[str]:
        return {c.key for c in self.channels}


def _component_key(r: Reader, m: Map, where: str) -> ComponentKey | None:
    sup = m.get("supplier")
    part = m.get("part")
    if sup is None or scalar_text(sup) is None:
        r.error(m.loc, f"{where} is missing its 'supplier'. Every line needs one.")
        return None
    if part is None or scalar_text(part) is None:
        r.error(m.loc, f"{where} is missing its 'part' number.")
        return None
    return ComponentKey(str(scalar_text(sup)), str(scalar_text(part)))


def _read_bom_line(r: Reader, node: object, kit: str) -> BomLine | None:
    if not isinstance(node, Map):
        return None
    key = _component_key(r, node, f"A component line in kit {kit}")
    if key is None:
        return None
    qty_node = node.get("qty")
    qty = r.int_value(qty_node, f"The quantity for {key} in kit {kit}") if qty_node else 1
    if qty is None:
        return None
    if qty <= 0:
        r.error(node.loc, f"The quantity for {key} in kit {kit} must be at least 1.")
        return None
    channels: list[str] | None = None
    ch_node = node.get("channels")
    if ch_node is not None:
        if isinstance(ch_node, Seq):
            channels = [str(scalar_text(c) or "") for c in ch_node.items]
        else:
            r.error(ch_node.loc, f"'channels' on {key} in kit {kit} should be a list.")
    return BomLine(key, qty, channels, node.loc)


def _opt_int(r: Reader, node: Map, key: ComponentKey, field_name: str) -> tuple[int | None, bool]:
    """An optional integer field: (value, is_TODO_placeholder)."""
    fnode = node.get(field_name)
    if fnode is None or (isinstance(fnode, Scalar) and fnode.value is None):
        return None, False
    if is_todo(fnode):
        return None, True
    return r.int_value(fnode, f"{field_name} for {key}"), False


def _read_components(r: Reader, boms: Map) -> dict[ComponentKey, ComponentCfg]:
    out: dict[ComponentKey, ComponentCfg] = {}
    comps = boms.get("components")
    if not isinstance(comps, Seq):
        r.error(boms.loc, "The BOM file has no 'components' list.")
        return out
    for node in comps.items:
        if not isinstance(node, Map):
            r.error(node.loc, "Each component should be a mapping of fields.")
            continue
        maybe_key = _component_key(r, node, "A component")
        if maybe_key is None:
            continue
        key: ComponentKey = maybe_key
        name = scalar_text(node.get("name") or Scalar(None, node.loc)) or str(key)
        cls_node = node.get("class")
        cls = scalar_text(cls_node) if cls_node is not None else None

        upu_node = node.get("units_per_purchase_unit")
        upu_missing = upu_node is None
        upu: int | None = None
        if upu_node is not None and not (isinstance(upu_node, Scalar) and upu_node.value is None):
            upu = r.int_value(upu_node, f"units_per_purchase_unit for {key}")

        moq_min, _ = _opt_int(r, node, key, "moq_min")
        moq_inc, _ = _opt_int(r, node, key, "moq_increment")
        rp, rp_todo = _opt_int(r, node, key, "reorder_point")
        rt, rt_todo = _opt_int(r, node, key, "reorder_target")

        if key in out:
            r.error(node.loc, f"Component {key} is listed twice; identities must be unique.")
            continue
        out[key] = ComponentCfg(
            key=key,
            name=name,
            cls=cls,
            units_per_purchase_unit=upu,
            upu_missing=upu_missing,
            purchase_unit_name=scalar_text(
                node.get("purchase_unit_name") or Scalar(None, node.loc)
            ),
            purchase_asin=scalar_text(node.get("purchase_asin") or Scalar(None, node.loc)),
            moq_min=moq_min or 0,
            moq_increment=moq_inc or 1,
            reorder_point=rp,
            reorder_target=rt,
            thresholds_todo=rp_todo or rt_todo,
            loc=node.loc,
        )
    return out


def _read_kits(r: Reader, boms: Map) -> dict[str, KitCfg]:
    out: dict[str, KitCfg] = {}
    kits = boms.get("kits")
    if not isinstance(kits, Map):
        r.error(boms.loc, "The BOM file has no 'kits' mapping.")
        return out
    for kit_group, node in kits.entries.items():
        if not isinstance(node, Map):
            r.error(kits.key_locs[kit_group], f"Kit {kit_group} should be a mapping.")
            continue
        name = scalar_text(node.get("name") or Scalar(None, node.loc)) or kit_group
        aliases: dict[str, str] = {}
        alias_locs: dict[str, Loc] = {}
        alias_node = node.get("aliases")
        if isinstance(alias_node, Map):
            for ch, sku_node in alias_node.entries.items():
                aliases[ch] = scalar_text(sku_node) or "TODO"
                alias_locs[ch] = sku_node.loc
        else:
            r.error(node.loc, f"Kit {kit_group} has no 'aliases' mapping of channel SKUs.")
        lines: list[BomLine] = []
        comp_node = node.get("components")
        if isinstance(comp_node, Seq):
            for line_node in comp_node.items:
                line = _read_bom_line(r, line_node, kit_group)
                if line is not None:
                    lines.append(line)
        else:
            r.error(node.loc, f"Kit {kit_group} has no 'components' list.")
        pouch: BomLine | None = None
        pouch_node = node.get("pouch")
        if pouch_node is not None:
            pouch = _read_bom_line(r, pouch_node, kit_group)
        out[kit_group] = KitCfg(
            kit_group=kit_group,
            name=name,
            aliases=aliases,
            alias_locs=alias_locs,
            components=lines,
            pouch=pouch,
            build_blocked=scalar_text(node.get("build_blocked") or Scalar(None, node.loc)),
            loc=kits.key_locs[kit_group],
        )
    return out


def _read_suppliers(r: Reader, boms: Map) -> dict[str, SupplierCfg]:
    out: dict[str, SupplierCfg] = {}
    sups = boms.get("suppliers")
    if not isinstance(sups, Map):
        r.error(boms.loc, "The BOM file has no 'suppliers' mapping.")
        return out
    for key, node in sups.entries.items():
        if not isinstance(node, Map):
            r.error(sups.key_locs[key], f"Supplier {key} should be a mapping.")
            continue
        lt_node = node.get("lead_time_weeks")
        lt: Fraction | None = None
        lt_todo = False
        if lt_node is not None:
            if is_todo(lt_node):
                lt_todo = True
            elif isinstance(lt_node, Scalar) and isinstance(lt_node.value, int | float):
                lt = Fraction(str(lt_node.value))
        out[key] = SupplierCfg(
            key=key,
            name=scalar_text(node.get("name") or Scalar(None, node.loc)) or key,
            acquisition=scalar_text(node.get("acquisition") or Scalar(None, node.loc)) or "none",
            lead_time_weeks=lt,
            lead_time_todo=lt_todo,
            loc=sups.key_locs[key],
        )
    return out


def _read_standalone_prep(r: Reader, boms: Map) -> list[StandalonePrep]:
    out: list[StandalonePrep] = []
    node = boms.get("standalone_fba_prep")
    if node is None:
        return out
    if not isinstance(node, Seq):
        r.error(node.loc, "'standalone_fba_prep' should be a list.")
        return out
    for row in node.items:
        if not isinstance(row, Map):
            continue
        consumes = row.get("consumes")
        if not isinstance(consumes, Map):
            r.error(row.loc, "Each standalone_fba_prep row needs a 'consumes' mapping.")
            continue
        key = _component_key(r, consumes, "A standalone_fba_prep 'consumes' entry")
        if key is None:
            continue
        qty_node = consumes.get("qty")
        qty = r.int_value(qty_node, f"The qty for prep item {key}") if qty_node else 1
        if qty is None:
            continue
        applies = scalar_text(row.get("applies_to") or Scalar(None, row.loc))
        sku = scalar_text(row.get("sku") or Scalar(None, row.loc))
        out.append(
            StandalonePrep(
                sku=sku,
                applies_to_all=applies == "all_fba_units",
                key=key,
                qty=qty,
                loc=row.loc,
            )
        )
    return out


def _read_parking_lot(r: Reader, boms: Map) -> list[ParkingItem]:
    out: list[ParkingItem] = []
    node = boms.get("parking_lot")
    if node is None:
        return out
    if not isinstance(node, Seq):
        r.error(node.loc, "'parking_lot' should be a list.")
        return out
    for row in node.items:
        if not isinstance(row, Map):
            continue
        pl_id = scalar_text(row.get("id") or Scalar(None, row.loc))
        item = scalar_text(row.get("item") or Scalar(None, row.loc))
        if pl_id is None or item is None:
            r.error(row.loc, "Each parking-lot entry needs an 'id' and an 'item'.")
            continue
        out.append(
            ParkingItem(
                pl_id=pl_id,
                item=item,
                detail=scalar_text(row.get("detail") or Scalar(None, row.loc)),
                blocks=scalar_text(row.get("blocks") or Scalar(None, row.loc)),
                loc=row.loc,
            )
        )
    return out


def _read_entity_file(r: Reader, path: Path, entity_id: str) -> tuple[str, str, list[ChannelCfg]]:
    m = load_file(path)
    legal = scalar_text(m.get("legal_name") or Scalar(None, m.loc)) or entity_id
    tz = scalar_text(m.get("timezone") or Scalar(None, m.loc)) or "UTC"
    channels: list[ChannelCfg] = []
    ch_node = m.get("channels")
    if isinstance(ch_node, Seq):
        for row in ch_node.items:
            if not isinstance(row, Map):
                continue
            key = scalar_text(row.get("key") or Scalar(None, row.loc))
            name = scalar_text(row.get("name") or Scalar(None, row.loc))
            fulfillment = scalar_text(row.get("fulfillment") or Scalar(None, row.loc))
            if key is None or name is None or fulfillment is None:
                r.error(row.loc, "Each channel needs 'key', 'name' and 'fulfillment'.")
                continue
            has_history = False
            hh = row.get("has_history")
            if isinstance(hh, Scalar) and isinstance(hh.value, bool):
                has_history = hh.value
            channels.append(ChannelCfg(key, name, fulfillment, has_history, row.loc))
    else:
        r.error(m.loc, f"Entity file {path} has no 'channels' list.")
    return legal, tz, channels


def _frac(r: Reader, m: Map, key: str, default: Fraction) -> Fraction:
    node = m.get(key)
    if node is None:
        return default
    if isinstance(node, Scalar) and isinstance(node.value, int | float):
        return Fraction(str(node.value))
    r.error(node.loc, f"'{key}' should be a number.")
    return default


def _int_param(r: Reader, m: Map, key: str, default: int) -> int:
    node = m.get(key)
    if node is None:
        return default
    if isinstance(node, Scalar) and isinstance(node.value, int):
        return node.value
    r.error(node.loc, f"'{key}' should be a whole number.")
    return default


def _read_shannon_file(r: Reader, path: Path) -> ShannonCfg:
    m = load_file(path)
    identity = m.get("identity")
    from_name = "Shannon"
    from_address = ""
    if isinstance(identity, Map):
        from_name = scalar_text(identity.get("from_name") or Scalar(None, m.loc)) or from_name
        from_address = (
            scalar_text(identity.get("from_address") or Scalar(None, m.loc)) or from_address
        )
    recipients: dict[str, dict[str, str]] = {}
    rec_node = m.get("recipients")
    if isinstance(rec_node, Map):
        for role, node in rec_node.entries.items():
            entry: dict[str, str] = {}
            if isinstance(node, Map):
                for k, v in node.entries.items():
                    text = scalar_text(v)
                    if text is not None:
                        entry[k] = text
            recipients[role] = entry
    else:
        r.error(m.loc, f"{path} has no 'recipients' mapping — roles cannot resolve.")

    params = Params()
    p_node = m.get("parameters")
    if isinstance(p_node, Map):
        params = Params(
            velocity_window_days=_int_param(r, p_node, "velocity_window_days", 90),
            cover_target_weeks=_frac(r, p_node, "cover_target_weeks", Fraction(7)),
            safety_stock_weeks=_frac(r, p_node, "safety_stock_weeks", Fraction(0)),
            round_up_to_nearest=_int_param(r, p_node, "round_up_to_nearest", 5),
            mf_floor_weeks=_frac(r, p_node, "mf_floor_weeks", Fraction(2)),
            fba_cover_weeks=_frac(r, p_node, "fba_cover_weeks", Fraction(8)),
            walmart_reserve_units=_int_param(r, p_node, "walmart_reserve_units", 0),
            box_min=_int_param(r, p_node, "box_min", 5),
            box_max=_int_param(r, p_node, "box_max", 10),
            overship_tolerance=_int_param(r, p_node, "overship_tolerance", 0),
        )
    else:
        r.error(m.loc, f"{path} has no 'parameters' section.")

    cadence = 6
    groups: list[OpsGroup] = []
    ops = m.get("ops_reminders")
    if isinstance(ops, Map):
        cadence = _int_param(r, ops, "cadence_weeks", 6)
        g_node = ops.get("groups")
        if isinstance(g_node, Map):
            for gname, gval in g_node.entries.items():
                if not isinstance(gval, Map):
                    continue
                roles: list[str] = []
                roles_node = gval.get("recipients")
                if isinstance(roles_node, Seq):
                    roles = [str(scalar_text(x) or "") for x in roles_node.items]
                comps: list[ComponentKey] = []
                comps_node = gval.get("components")
                if isinstance(comps_node, Seq):
                    for row in comps_node.items:
                        if isinstance(row, Map):
                            key = _component_key(r, row, f"An ops-reminder line in {gname}")
                            if key is not None:
                                comps.append(key)
                groups.append(OpsGroup(gname, roles, comps, gval.loc))
    return ShannonCfg(
        from_name=from_name,
        from_address=from_address,
        recipients=recipients,
        params=params,
        ops_cadence_weeks=cadence,
        ops_groups=groups,
        loc=m.loc,
    )


def _read_products_file(r: Reader, path: Path) -> list[StandaloneProduct]:
    out: list[StandaloneProduct] = []
    if not path.exists():
        return out
    m = load_file(path)
    node = m.get("standalone")
    if not isinstance(node, Seq):
        return out
    for row in node.items:
        if not isinstance(row, Map):
            continue
        sku = scalar_text(row.get("sku") or Scalar(None, row.loc))
        key = _component_key(r, row, "A standalone product")
        if sku is None or key is None:
            r.error(row.loc, "Each standalone product needs 'sku', 'supplier' and 'part'.")
            continue
        out.append(
            StandaloneProduct(
                sku=sku,
                key=key,
                sales_asin=scalar_text(row.get("sales_asin") or Scalar(None, row.loc)),
                loc=row.loc,
            )
        )
    return out


def load_entity_config(config_dir: Path, entity_id: str) -> EntityConfig:
    """Load every config file for one entity, collecting issues."""
    r = Reader()
    entity_path = config_dir / "entities" / f"{entity_id}.yaml"
    boms_path = config_dir / entity_id / "boms.yaml"
    shannon_path = config_dir / entity_id / "shannon.yaml"
    products_path = config_dir / entity_id / "products.yaml"

    legal, tz, channels = _read_entity_file(r, entity_path, entity_id)
    boms = load_file(boms_path)
    shannon = _read_shannon_file(r, shannon_path)

    bom_version = scalar_text(boms.get("bom_version") or Scalar(None, boms.loc)) or ""
    if not bom_version:
        r.error(boms.loc, "The BOM file has no 'bom_version' — every report must print one.")
    pack_mode = "discover_and_confirm"
    psp = boms.get("pack_size_policy")
    if isinstance(psp, Map):
        pack_mode = scalar_text(psp.get("mode") or Scalar(None, psp.loc)) or pack_mode

    texts: dict[str, str] = {}
    for p in (entity_path, boms_path, shannon_path, products_path):
        if p.exists():
            texts[p.name] = p.read_text(encoding="utf-8")

    return EntityConfig(
        entity_id=entity_id,
        legal_name=legal,
        timezone=tz,
        channels=channels,
        bom_version=bom_version,
        pack_size_mode=pack_mode,
        suppliers=_read_suppliers(r, boms),
        components=_read_components(r, boms),
        kits=_read_kits(r, boms),
        standalone_prep=_read_standalone_prep(r, boms),
        parking_lot=_read_parking_lot(r, boms),
        standalone_products=_read_products_file(r, products_path),
        shannon=shannon,
        issues=r.issues,
        config_texts=texts,
    )
