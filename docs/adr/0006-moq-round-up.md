# ADR-0006: MOQ rounding always rounds up, never to nearest

**Status:** Accepted · **Date:** 2026-08-20

## Context
NAR imposes per-component minimums and increments (CAT: min 400 step 200;
HyFin: min 750 step 150). A net requirement of 410 CATs could round to 400
(nearest) or 600 (up).

## Decision
`moq_round(q) = moq_min` for `0 < q ≤ moq_min`, else
`moq_min + ceil((q − moq_min)/increment) × increment`. 410 → 600.

## Consequences
- Never under-covers computed need on safety-critical items; over-order is
  bounded by one increment and shown per line on the report (raw vs.
  rounded).
- Ties up somewhat more cash. If that matters, the honest lever is
  `cover_target_weeks`, not nearest-rounding that hides a shortfall.
