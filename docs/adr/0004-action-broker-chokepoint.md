# ADR-0004: All external side effects through one ActionBroker

**Status:** Accepted · **Date:** 2026-08-20

## Context
The system's safety story depends on there being exactly one path from
agent intent to the outside world, with policy and audit on that path.

## Decision
Every external effect is an ActionProposal executed by the broker after a
capability check, policy tiering, and write-ahead audit. Proposals carry
content-derived idempotency keys (UNIQUE column) so retries cannot re-stage
a cart or re-send an email. CI fails if agent modules import integration
clients (import-linter contract + path check restricting integration SDKs
to `broker/executors/`).

## Consequences
- Approval, tiering, reversal metadata, and audit are uniform across every
  action type, present and future.
- Integration work is slightly heavier: each new effect needs an executor
  and a registry entry, not an inline call. That friction is the point.
