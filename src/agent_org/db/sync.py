"""Copy the validated configuration into the entity-scoped tables.

The YAML files stay the source of truth. This mirror exists so that a
report, an audit row or a future dashboard can join against the same parts
list the run used, and so the database's own constraints (a component must
have a class, a BOM quantity must be positive) act as a second line of
defence behind `shannon validate-config`.

Every statement here runs inside an `entity_session`, so row-level
security applies to the sync exactly as it applies to everything else.
"""

from __future__ import annotations

import psycopg

from agent_org.config.models import ComponentClass, LoadedConfig


def sync_config(
    conn: psycopg.Connection[tuple[object, ...]], config: LoadedConfig
) -> dict[str, int]:
    """Mirror the configuration into the tables and report what was written."""
    _sync_channels(conn, config)
    supplier_ids = _sync_suppliers(conn, config)
    component_ids = _sync_components(conn, config, supplier_ids)
    _sync_products(conn, config, component_ids)
    lines = _sync_boms(conn, config, component_ids)
    return {
        "channels": len(config.entity.channels),
        "suppliers": len(supplier_ids),
        "components": len(component_ids),
        "kits": len(config.boms.kits),
        "bom_lines": lines,
    }


def _sync_channels(conn: psycopg.Connection[tuple[object, ...]], config: LoadedConfig) -> None:
    for channel in config.entity.channels:
        conn.execute(
            """
            INSERT INTO channels (entity_id, name, fulfillment, has_history)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_id, name) DO UPDATE
               SET fulfillment = EXCLUDED.fulfillment,
                   has_history = EXCLUDED.has_history
            """,
            (config.entity_id, channel.name, channel.fulfillment, channel.has_history),
        )


def _sync_suppliers(
    conn: psycopg.Connection[tuple[object, ...]], config: LoadedConfig
) -> dict[str, str]:
    ids: dict[str, str] = {}
    for key, supplier in config.boms.suppliers.items():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO suppliers (entity_id, name, capabilities, lead_time_weeks)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (entity_id, name) DO UPDATE
                   SET capabilities    = EXCLUDED.capabilities,
                       lead_time_weeks = EXCLUDED.lead_time_weeks
                RETURNING id
                """,
                (
                    config.entity_id,
                    key,
                    sorted(capability.value for capability in supplier.capabilities),
                    float(supplier.lead_time_weeks)
                    if supplier.lead_time_weeks is not None
                    else None,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            ids[key] = str(row[0])
    return ids


def _sync_components(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    supplier_ids: dict[str, str],
) -> dict[str, str]:
    ids: dict[str, str] = {}
    for key, component in config.boms.components.items():
        supplier_id = supplier_ids.get(key.supplier)
        if supplier_id is None:
            # An unknown supplier is already an error from validate-config;
            # skip rather than inventing a supplier row for it.
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO components (
                    entity_id, supplier_id, supplier_part_no, name, class,
                    purchase_asin, moq_min, moq_increment, units_per_purchase_unit,
                    purchase_unit_name, reorder_point, reorder_target,
                    cover_target_weeks, safety_stock_weeks)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, supplier_id, supplier_part_no) DO UPDATE
                   SET name                    = EXCLUDED.name,
                       class                   = EXCLUDED.class,
                       purchase_asin           = EXCLUDED.purchase_asin,
                       moq_min                 = EXCLUDED.moq_min,
                       moq_increment           = EXCLUDED.moq_increment,
                       units_per_purchase_unit = EXCLUDED.units_per_purchase_unit,
                       purchase_unit_name      = EXCLUDED.purchase_unit_name,
                       reorder_point           = EXCLUDED.reorder_point,
                       reorder_target          = EXCLUDED.reorder_target,
                       cover_target_weeks      = EXCLUDED.cover_target_weeks,
                       safety_stock_weeks      = EXCLUDED.safety_stock_weeks
                RETURNING id
                """,
                (
                    config.entity_id,
                    supplier_id,
                    key.part,
                    component.name,
                    component.component_class.value,
                    component.purchase_asin,
                    component.moq_min,
                    max(component.moq_increment, 1),
                    component.units_per_purchase_unit,
                    component.purchase_unit_name,
                    component.reorder_point,
                    component.reorder_target,
                    float(component.cover_target_weeks)
                    if component.cover_target_weeks is not None
                    else None,
                    float(component.safety_stock_weeks)
                    if component.safety_stock_weeks is not None
                    else None,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            ids[str(key)] = str(row[0])
    return ids


def _sync_products(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    component_ids: dict[str, str],
) -> None:
    for kit_group, kit in config.boms.kits.items():
        for channel_key, sku in kit.aliases.items():
            if sku is None:
                continue  # unresolved alias: reported, never invented
            conn.execute(
                """
                INSERT INTO products (entity_id, sku, name, product_type, kit_group,
                                      channel_alias, status)
                VALUES (%s, %s, %s, 'hmz_kit', %s, %s, 'active')
                ON CONFLICT (entity_id, sku) DO UPDATE
                   SET name          = EXCLUDED.name,
                       kit_group     = EXCLUDED.kit_group,
                       channel_alias = EXCLUDED.channel_alias
                """,
                (config.entity_id, sku, kit.name, kit_group, channel_key),
            )


def _sync_boms(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    component_ids: dict[str, str],
) -> int:
    version = config.boms.bom_version
    written = 0
    for kit_group, kit in config.boms.kits.items():
        for line in kit.lines:
            component_id = component_ids.get(str(line.component))
            component = config.boms.components.get(line.component)
            if component_id is None or component is None:
                continue  # dangling reference: reported by validate-config
            if component.component_class is ComponentClass.OPS_CONSUMABLE:
                continue  # ops consumables are never part of a counted BOM
            conn.execute(
                """
                INSERT INTO boms (entity_id, kit_group, component_id, qty, channels,
                                  bom_version)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, kit_group, component_id, bom_version) DO UPDATE
                   SET qty      = EXCLUDED.qty,
                       channels = EXCLUDED.channels
                """,
                (
                    config.entity_id,
                    kit_group,
                    component_id,
                    line.qty,
                    sorted(line.channels) if line.channels else None,
                    version,
                ),
            )
            written += 1
    return written


__all__ = ["sync_config"]
