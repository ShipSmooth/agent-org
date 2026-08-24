-- Some suppliers publish no item numbers at all. Orca Tactical Gear is the
-- first: ORCA-MOLLE-EMT-COYOTE is a reference we minted so the component has
-- an identity, and quoting it on a purchase order would name a SKU Orca has
-- never heard of. The flag says which side of that line a part number is on,
-- so nothing downstream has to infer it from the shape of the string.

ALTER TABLE components
    ADD COLUMN part_is_internal_reference BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN components.part_is_internal_reference IS
    'TRUE where supplier_part_no is ours, not the supplier''s. Order by name.';
