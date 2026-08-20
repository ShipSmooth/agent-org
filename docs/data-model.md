# Data model — Postgres DDL

Postgres 16. Every business table carries `entity_id` and is protected by
forced row-level security. Migrations run as role `agent_org_migrator`
(table owner); the application connects as `agent_org_app` (no BYPASSRLS,
not owner). `updated_at` maintained by trigger (omitted for brevity).

```sql
-- ============ entities & registry ============
CREATE TABLE entities (
    id            TEXT PRIMARY KEY,              -- slug: 'ithrive', 'limazulu', 'shipsmooth'
    legal_name    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active', 'dormant')),
    timezone      TEXT NOT NULL,
    config        JSONB NOT NULL DEFAULT '{}',   -- synced from config/entities/*.yaml
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- entities itself is not RLS-scoped: it is the registry the scoping keys into.

-- ============ suppliers ============
CREATE TABLE suppliers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    name          TEXT NOT NULL,                 -- 'NAR', 'Dynarex', ...
    capabilities  TEXT[] NOT NULL DEFAULT '{report_only}',
                  -- of: read_catalog, read_order_history, stage_cart, purchase, report_only
    lead_time_weeks NUMERIC(4,1),
    config        JSONB NOT NULL DEFAULT '{}',
    UNIQUE (entity_id, name)
);

-- ============ components (purchasable/consumable parts) ============
CREATE TABLE components (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id      TEXT NOT NULL REFERENCES entities(id),
    sku            TEXT NOT NULL,                -- '30-0001', 'PKG-IFAK-POUCH-BLK'
    name           TEXT NOT NULL,
    supplier_id    UUID REFERENCES suppliers(id),  -- NULL = unresolved (flagged on gap list)
    supplier_part_no TEXT,
    regime         TEXT NOT NULL CHECK (regime IN ('forecast', 'reorder_point')),
    moq_min        INT NOT NULL DEFAULT 0,
    moq_increment  INT NOT NULL DEFAULT 1 CHECK (moq_increment >= 1),
    reorder_point  INT,                          -- reorder_point regime only
    reorder_target INT,
    cover_target_weeks  NUMERIC(4,1),            -- NULL = entity default
    safety_stock_weeks  NUMERIC(4,1),
    UNIQUE (entity_id, sku)
);

-- ============ products (sellable SKUs) ============
CREATE TABLE products (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id      TEXT NOT NULL REFERENCES entities(id),
    sku            TEXT NOT NULL,
    name           TEXT NOT NULL,
    product_type   TEXT NOT NULL CHECK (product_type IN
                     ('nar_finished_kit', 'hmz_kit', 'nar_component_standalone')),
    component_id   UUID REFERENCES components(id), -- standalone sales of a component
    kit_group      TEXT,                          -- groups channel aliases of one HMZ kit
    channel_alias  TEXT,                          -- 'fba' | 'fbm' | ... within kit_group
    status         TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'draft', 'archived')),
    UNIQUE (entity_id, sku)
);

-- ============ boms (synced from config/<entity>/boms.yaml) ============
CREATE TABLE boms (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    kit_group     TEXT NOT NULL,                 -- matches products.kit_group
    component_id  UUID NOT NULL REFERENCES components(id),
    qty           INT NOT NULL CHECK (qty > 0),
    bom_version   TEXT NOT NULL,                 -- date stamp from config
    UNIQUE (entity_id, kit_group, component_id, bom_version)
);

-- ============ channels ============
CREATE TABLE channels (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    name          TEXT NOT NULL,   -- 'amazon_fba', 'amazon_fbm', 'shopify', 'walmart_sf', 'walmart_wfs'
    fulfillment   TEXT NOT NULL CHECK (fulfillment IN ('fba', 'merchant', 'wfs')),
    has_history   BOOLEAN NOT NULL DEFAULT false,  -- Walmart: false until real velocity exists
    UNIQUE (entity_id, name)
);

-- ============ tasks (the queue) ============
CREATE TABLE tasks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    kind          TEXT NOT NULL,                 -- 'replenishment'
    state         TEXT NOT NULL DEFAULT 'QUEUED' CHECK (state IN
                    ('QUEUED','RUNNING','WAITING_APPROVAL','SUCCEEDED',
                     'FAILED','REJECTED','EXPIRED')),
    schedule_slot TEXT NOT NULL,                 -- 'replenishment/2026-W34'
    attempts      INT NOT NULL DEFAULT 0,
    max_attempts  INT NOT NULL DEFAULT 3,
    heartbeat_at  TIMESTAMPTZ,
    payload       JSONB NOT NULL DEFAULT '{}',
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, kind, schedule_slot)      -- one run per business occurrence
);

-- ============ action_proposals ============
CREATE TABLE action_proposals (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id        TEXT NOT NULL REFERENCES entities(id),
    task_id          UUID NOT NULL REFERENCES tasks(id),
    action_type      TEXT NOT NULL,
    payload          JSONB NOT NULL,
    data_snapshot_at TIMESTAMPTZ NOT NULL,
    tier             INT NOT NULL CHECK (tier BETWEEN 0 AND 3),
    fired_triggers   JSONB NOT NULL DEFAULT '[]',
    reversible       TEXT NOT NULL CHECK (reversible IN ('yes','no','window')),
    status           TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (status IN
                       ('PROPOSED','PENDING_APPROVAL','PENDING_CONFIRMATION',
                        'APPROVED','APPROVED_AUTO','EXECUTING','EXECUTED',
                        'FAILED','REJECTED','EXPIRED','REVERSED')),
    idempotency_key  TEXT NOT NULL,
    result           JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,
    executed_at      TIMESTAMPTZ,
    UNIQUE (idempotency_key)
);

-- ============ approvals ============
CREATE TABLE approvals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    proposal_id   UUID NOT NULL REFERENCES action_proposals(id),
    stage         TEXT NOT NULL CHECK (stage IN ('approval', 'confirmation')), -- Tier 3 = both
    decision      TEXT NOT NULL CHECK (decision IN ('approved', 'denied')),
    decided_by    TEXT NOT NULL,                 -- 'zach' (signed token identity)
    channel       TEXT NOT NULL CHECK (channel IN ('email', 'sms', 'dashboard')),
    token_id      TEXT NOT NULL,                 -- the signed approval token used
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (proposal_id, stage)
);

-- ============ audit_log (append-only) ============
CREATE TABLE audit_log (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor         TEXT NOT NULL,                 -- 'agent:replenishment', 'broker', 'human:zach'
    task_id       UUID,
    proposal_id   UUID,
    event         TEXT NOT NULL,                 -- 'task.state', 'proposal.status', ...
    phase         TEXT NOT NULL CHECK (phase IN ('intent', 'outcome')),  -- write BEFORE execute, update AFTER
    detail        JSONB NOT NULL DEFAULT '{}'
);
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM agent_org_app;  -- append-only, enforced
-- ('outcome' is a second INSERT referencing the intent row id in detail, not an UPDATE)

-- ============ agent_runs (transcripts / memory) ============
CREATE TABLE agent_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    task_id       UUID NOT NULL REFERENCES tasks(id),
    agent_kind    TEXT NOT NULL,
    model_calls   JSONB NOT NULL DEFAULT '[]',   -- model, tokens, cost per call
    step_count    INT NOT NULL DEFAULT 0,
    wall_ms       INT,
    transcript    JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ order_history (feeds anomaly triggers) ============
CREATE TABLE order_history (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    supplier_id   UUID NOT NULL REFERENCES suppliers(id),
    ordered_at    TIMESTAMPTZ NOT NULL,
    total_usd     NUMERIC(12,2) NOT NULL,
    total_units   INT NOT NULL,
    lines         JSONB NOT NULL,                -- [{sku, qty, unit_price}]
    source        TEXT NOT NULL CHECK (source IN ('manual_backfill', 'staged_cart', 'purchase')),
    proposal_id   UUID REFERENCES action_proposals(id)
);
```

## Row-level security

Applied identically to every table above **except `entities`**:

```sql
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
      'suppliers','components','products','boms','channels','tasks',
      'action_proposals','approvals','audit_log','agent_runs','order_history']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($f$
      CREATE POLICY %I ON %I
        USING (entity_id = current_setting('app.entity_id'))
        WITH CHECK (entity_id = current_setting('app.entity_id'))
    $f$, t || '_entity_isolation', t);
  END LOOP;
END $$;
```

- `current_setting('app.entity_id')` is deliberately the one-argument form:
  a connection that never ran `SET LOCAL app.entity_id = ...` gets a raised
  error on any query — loud failure, never another entity's rows and never
  a silently empty result (see docs/multi-entity.md).
- `agent_org_app` has no BYPASSRLS and owns no tables; `FORCE` covers the
  owner anyway.
- Cross-entity reporting (if ever needed) uses a dedicated read-only role
  with explicit per-table SELECT policies — not by weakening these.
