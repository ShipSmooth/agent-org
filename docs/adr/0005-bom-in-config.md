# ADR-0005: BOMs live in version-controlled config, not Veeqo

**Status:** Accepted · **Date:** 2026-08-20

## Context
Veeqo does not support bundles for FBA: HMZ kits sold via FBA are separate
simple products with **no link** to the components they consume. Veeqo FBM
bundles do carry BOMs, so the alternatives were (a) config for FBA + Veeqo
for FBM (two sources of truth) or (b) config for everything.

## Decision
The full BOM for every HMZ kit lives in `config/<entity>/boms.yaml`,
date-versioned, with an `aliases` map joining each kit to its
channel-specific SKUs (including the Veeqo FBA simple product). Veeqo FBM
bundles are read only as a weekly cross-check; disagreement raises a
data-quality warning and config wins. A kit selling with no BOM entry is a
hard run failure. Seven kits, 61 BOM lines today, including both
own-assembled IFAKs: the full IFAK with CAT Gen 7 & HyFin (four colourway
SKUs: IFAK-CAT-BLACK/-GREEN/-COYOTE/-MULTICAM; 13 lines) and the Compact
IFAK Trauma Kit (6 lines, different carrier). Every BOM line carries the
component's `(supplier, supplier_part_number)` identity and its required
class; `non_stocked` lines stay in the BOM for kit description and cost
but never produce a purchase.

## Consequences
- One source of truth, reviewable in a PR diff, versioned on every report.
- Keeping it current is a human duty; drift is surfaced (cross-check,
  hard failure on missing entries) but not self-healing.
