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
hard run failure.

## Consequences
- One source of truth, reviewable in a PR diff, versioned on every report.
- Keeping it current is a human duty; drift is surfaced (cross-check,
  hard failure on missing entries) but not self-healing.
