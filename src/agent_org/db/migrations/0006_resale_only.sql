-- Forty-two of the products Zach sells are bought complete from NAR and
-- resold as they come: finished kits and standalone components. They are
-- components because he buys them, but they are never inside anything, so
-- "used by no kit" is their normal state rather than a missing kit line.
-- The flag says so once, here, rather than being inferred from a supplier
-- name or a part-number shape anywhere downstream.

ALTER TABLE components
    ADD COLUMN resale_only BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN components.resale_only IS
    'TRUE for a product bought complete and resold. Forecast from its own '
    'sales; never exploded from a kit and never expected on a BOM line.';
