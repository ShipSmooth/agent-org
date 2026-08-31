-- 0010 — a line that failed and then went in has to end up saying it went
-- in.
--
-- 0009 recorded each SKU once per week per mode and left it there:
-- `ON CONFLICT DO NOTHING`. A week whose first live attempt failed every
-- line therefore kept its FAILED rows when the retry actually added them,
-- and FAILED is not a status the executor skips — so the next retry added
-- the same tourniquets on top of the ones already in the cart, which is
-- exactly what this table exists to prevent.
--
-- The row is now allowed to move on from FAILED, and only from FAILED (or
-- SKIPPED). ADDED never changes: what is in the cart is in the cart, and
-- nothing later may overwrite the record of it.
ALTER TABLE cart_stagings ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();

COMMENT ON COLUMN cart_stagings.updated_at IS
    'When this row last changed. A row only ever changes by an attempt '
    'that failed being followed by one that succeeded.';

GRANT UPDATE ON cart_stagings TO agent_org_app;
