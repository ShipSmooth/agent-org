-- 0001_schema.sql — the tables from docs/data-model.md.
-- Runs as agent_org_migrator (the owner). The application connects as
-- agent_org_app, which owns nothing and cannot bypass row-level security.

CREATE TABLE entities (
    id            TEXT PRIMARY KEY,
    legal_name    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active', 'dormant')),
    timezone      TEXT NOT NULL,
    config        JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- entities itself is not RLS-scoped: it is the registry the scoping keys into.

CREATE TABLE suppliers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    name            TEXT NOT NULL,
    capabilities    TEXT[] NOT NULL DEFAULT '{report_only}',
    lead_time_weeks NUMERIC(4,1),
    config          JSONB NOT NULL DEFAULT '{}',
    UNIQUE (entity_id, name)
);

CREATE TABLE components (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id               TEXT NOT NULL REFERENCES entities(id),
    supplier_id             UUID NOT NULL REFERENCES suppliers(id),
    supplier_part_no        TEXT NOT NULL,
    name                    TEXT NOT NULL,
    class                   TEXT NOT NULL CHECK (class IN
                              ('forecast', 'reorder_point', 'non_stocked', 'ops_consumable')),
    purchase_asin           TEXT,
    moq_min                 INT NOT NULL DEFAULT 0,
    moq_increment           INT NOT NULL DEFAULT 1 CHECK (moq_increment >= 1),
    units_per_purchase_unit INT CHECK (units_per_purchase_unit >= 1),
    purchase_unit_name      TEXT,
    reorder_point           INT,
    reorder_target          INT,
    cover_target_weeks      NUMERIC(4,1),
    safety_stock_weeks      NUMERIC(4,1),
    UNIQUE (entity_id, supplier_id, supplier_part_no)
);

CREATE TABLE products (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id      TEXT NOT NULL REFERENCES entities(id),
    sku            TEXT NOT NULL,
    name           TEXT NOT NULL,
    product_type   TEXT NOT NULL CHECK (product_type IN
                     ('nar_finished_kit', 'hmz_kit', 'nar_component_standalone')),
    sales_asin     TEXT,
    component_id   UUID REFERENCES components(id),
    kit_group      TEXT,
    channel_alias  TEXT,
    status         TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'draft', 'archived')),
    UNIQUE (entity_id, sku)
);

CREATE TABLE boms (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    kit_group     TEXT NOT NULL,
    component_id  UUID NOT NULL REFERENCES components(id),
    qty           INT NOT NULL CHECK (qty > 0),
    channels      TEXT[],
    bom_version   TEXT NOT NULL,
    UNIQUE (entity_id, kit_group, component_id, bom_version)
);

CREATE TABLE channels (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    name          TEXT NOT NULL,
    fulfillment   TEXT NOT NULL CHECK (fulfillment IN ('fba', 'merchant', 'wfs')),
    has_history   BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (entity_id, name)
);

CREATE TABLE tasks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    kind          TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'QUEUED' CHECK (state IN
                    ('QUEUED','RUNNING','WAITING_APPROVAL','SUCCEEDED',
                     'FAILED','REJECTED','EXPIRED')),
    schedule_slot TEXT NOT NULL,
    attempts      INT NOT NULL DEFAULT 0,
    max_attempts  INT NOT NULL DEFAULT 3,
    heartbeat_at  TIMESTAMPTZ,
    payload       JSONB NOT NULL DEFAULT '{}',
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, kind, schedule_slot)
);

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

-- The approvals table exists so the audit trail has somewhere to land in a
-- later phase. Phase 1 never writes a row: nothing above Tier 0 runs, so
-- nothing is ever put up for approval.
CREATE TABLE approvals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    proposal_id   UUID NOT NULL REFERENCES action_proposals(id),
    stage         TEXT NOT NULL CHECK (stage IN ('approval', 'confirmation')),
    decision      TEXT NOT NULL CHECK (decision IN ('approved', 'denied')),
    decided_by    TEXT NOT NULL,
    channel       TEXT NOT NULL CHECK (channel IN ('email', 'sms', 'dashboard')),
    token_id      TEXT NOT NULL,
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (proposal_id, stage)
);

CREATE TABLE audit_log (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor         TEXT NOT NULL,
    task_id       UUID,
    proposal_id   UUID,
    event         TEXT NOT NULL,
    phase         TEXT NOT NULL CHECK (phase IN ('intent', 'outcome')),
    detail        JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE agent_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    task_id       UUID NOT NULL REFERENCES tasks(id),
    agent_kind    TEXT NOT NULL,
    model_calls   JSONB NOT NULL DEFAULT '[]',
    step_count    INT NOT NULL DEFAULT 0,
    wall_ms       INT,
    transcript    JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE order_history (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    supplier_id   UUID NOT NULL REFERENCES suppliers(id),
    ordered_at    TIMESTAMPTZ NOT NULL,
    total_usd     NUMERIC(12,2) NOT NULL,
    total_units   INT NOT NULL,
    lines         JSONB NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('manual_backfill', 'staged_cart', 'purchase')),
    proposal_id   UUID REFERENCES action_proposals(id)
);

-- Reports are written to the database as well as to a file, so a report can
-- never be lost by someone tidying a folder, and so the next run can print
-- what changed since the last one.
CREATE TABLE reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    task_id       UUID NOT NULL REFERENCES tasks(id),
    agent_kind    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    bom_version   TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    parameters    JSONB NOT NULL DEFAULT '{}',
    lines         JSONB NOT NULL DEFAULT '[]',
    body          TEXT NOT NULL,
    file_path     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX tasks_state_idx ON tasks (entity_id, state);
CREATE INDEX audit_log_task_idx ON audit_log (entity_id, task_id);
CREATE INDEX reports_recent_idx ON reports (entity_id, agent_kind, created_at DESC);
