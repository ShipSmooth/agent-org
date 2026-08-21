# ADR-0008: Declarative policy tiers with default-deny

**Status:** Accepted · **Date:** 2026-08-20

## Context
Autonomy must be legible to a non-engineer owner and changeable without a
programmer. Hardcoded permission checks scatter and drift.

## Decision
Four tiers (0 silent, 1 notify-after, 2 approval, 3 approval + second
confirmation) defined in YAML: a global default file plus per-entity
overrides that inherit and may tighten but never loosen. Any action
matching no rule resolves to Tier 3 (default-deny). Anomaly triggers
($75k absolute; 150% of trailing 8-order average; any line 2× its trailing
average; total units 2× trailing average) escalate purchases to Tier 3.
Approvals expire to DENIED — never auto-approve — after 72 hours (Tier 2)
or 7 days (Tier 3), with a reminder at the halfway mark, and are re-raised
with fresh numbers. SMS (reply with the one-time code shown, never a bare
"Y") may decide Tier 2 and below; Tier 3 requires email plus the second
confirmation, never SMS.

## Consequences
- Thresholds are a config PR Zach can read; the resolved tier and fired
  triggers are printed on every approval so the "why" is visible.
- New action types are safe by construction: forgetting a rule yields
  maximum caution, not silent autonomy.
- With sparse order history the comparative triggers cannot fire, so all
  purchases stay Tier 3 until `min_history_orders` exist — conservative.
