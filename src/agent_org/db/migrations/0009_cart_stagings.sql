-- 0009 — what has been put in a supplier's cart, and what was only ever
-- going to be.
--
-- A cart line cannot be un-added by this system: Shannon has no capability
-- to remove one, by design. So the thing that stops a crashed run, a retry
-- or a second `shannon stage` from adding the same 40 tourniquets twice is
-- this table and its unique key, not care on the part of the caller.
--
-- The key is (supplier, schedule_slot, sku): one week's staging of one SKU
-- at one supplier is one business fact, however many times it is
-- attempted. A dry run is recorded under mode DRY_RUN and therefore never
-- blocks the live staging that follows it — it is a rehearsal, and a
-- rehearsal that suppressed the performance would be a trap.
CREATE TABLE cart_stagings (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    task_id       UUID NOT NULL REFERENCES tasks(id),
    supplier      TEXT NOT NULL,
    schedule_slot TEXT NOT NULL,
    sku           TEXT NOT NULL,
    quantity      INT NOT NULL CHECK (quantity > 0),
    units         INT NOT NULL,
    mode          TEXT NOT NULL CHECK (mode IN ('DRY_RUN', 'LIVE')),
    status        TEXT NOT NULL CHECK (status IN ('PLANNED', 'ADDED', 'FAILED', 'SKIPPED')),
    cart_id       TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (entity_id, supplier, schedule_slot, sku, mode)
);

COMMENT ON TABLE cart_stagings IS
    'One row per SKU per week per supplier per mode: what Shannon added to '
    'that cart, or would have. The unique key is what makes staging '
    'idempotent — a re-run finds the row and reports the line as already '
    'staged rather than adding it again, because a cart line cannot be '
    'taken back out.';

CREATE INDEX cart_stagings_slot_idx
    ON cart_stagings (entity_id, supplier, schedule_slot, created_at DESC);

ALTER TABLE cart_stagings ENABLE ROW LEVEL SECURITY;
ALTER TABLE cart_stagings FORCE ROW LEVEL SECURITY;
CREATE POLICY cart_stagings_entity_isolation ON cart_stagings
    USING (entity_id = current_setting('app.entity_id'))
    WITH CHECK (entity_id = current_setting('app.entity_id'));

GRANT SELECT, INSERT ON cart_stagings TO agent_org_app;
