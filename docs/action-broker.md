# ActionBroker — the single chokepoint

Every external side effect goes through the ActionBroker. **No second
path.** A direct integration call from agent code is a bug; CI enforces
this with an import-linter contract (agent modules may not import
integration clients) plus a grep-based check that only
`src/agent_org/broker/executors/` references integration SDKs or endpoints.

## ActionProposal model

```
ActionProposal
  id                UUID
  entity_id         TEXT                -- RLS-scoped like everything else
  task_id           UUID                -- the task that filed it
  action_type       TEXT                -- e.g. nar.stage_cart, notify.email,
                                        -- notify.sms, fba.create_inbound_plan,
                                        -- shopify.update_product
  payload           JSONB               -- full, typed arguments (the cart lines,
                                        -- the email body, ...)
  data_snapshot_at  TIMESTAMPTZ         -- when the numbers behind it were read
  tier              INT                 -- resolved by the policy engine at filing
  status            TEXT                -- see lifecycle
  idempotency_key   TEXT UNIQUE         -- see idempotency
  result            JSONB               -- executor output (e.g. freight quote)
  created_at / decided_at / executed_at TIMESTAMPTZ
```

## Lifecycle

```
PROPOSED ──policy──► APPROVED_AUTO (Tier 0/1) ─────────────┐
   │                                                        ▼
   ├──Tier 2──► PENDING_APPROVAL ──approve──► APPROVED ► EXECUTING ──► EXECUTED
   │                 │                                      │
   ├──Tier 3──► PENDING_APPROVAL ──approve──►               ├──► FAILED
   │            PENDING_CONFIRMATION ──confirm──► APPROVED  │
   │                 │                                      └──► REVERSED
   │                 ├──deny──► REJECTED                      (via reversal proposal)
   └──capability/policy violation──► REJECTED
                     └──ttl──► EXPIRED
```

Rules: capability check (docs/supplier-model.md) runs before policy; the
audit row is written at PROPOSED and at **every** transition, before the
transition's work executes (write-ahead, append-only); approvals older
than the proposal's TTL, or whose `data_snapshot_at` exceeds staleness
limits, land in EXPIRED and must be re-proposed with fresh numbers. Tier 1
executes immediately and notifies after; Tier 0 actions are internal and do
not pass through the broker at all (they have no external effect).

## Reversal strategy — per action type

Reversal is itself a proposal (audited, tiered), never an ad-hoc call.

| action_type | Reversible? | Strategy |
|---|---|---|
| `nar.stage_cart` | Yes | `nar.clear_cart` — browser automation empties the staged cart. Harmless even half-done; a cart costs nothing. |
| `notify.email` / `notify.sms` | No | Cannot unsend. Mitigation: correction message referencing the original's proposal id. This irreversibility is why anomalous notifications are still tiered, not free. |
| `fba.create_inbound_plan` (future) | Partly | Cancel the plan/shipment in Seller Central before carrier pickup; after pickup, irreversible → the *creation* is Tier 2 and flagged irreversible-after-pickup in the approval. |
| `shopify.update_product` (future) | Yes | Payload stores the prior field values; reversal writes them back. |
| `*.purchase` (not granted in v1) | No | Money movement is never assumed reversible. Default-deny keeps it Tier 3 even before any supplier grants the capability. |

Every action_type registers `reversible: yes|no|window` in the executor
registry; the approval email states it, so Zach knows what "yes" commits.

## Idempotency

`idempotency_key = sha256(entity_id, action_type, canonical(payload),
schedule_slot)` where `schedule_slot` identifies the business occurrence
(e.g. `replenishment/2026-W34`), not the attempt. The column is UNIQUE:

- A retried or crashed-and-reaped task re-filing the same proposal hits the
  unique constraint; the broker returns the **existing** proposal and its
  status instead of creating a duplicate. Same cart cannot be staged twice.
- Executors are two-phase: write `EXECUTING` + audit row, perform the
  effect, write `EXECUTED` + result. If a crash lands between phases, the
  reaper re-checks the external state where possible (e.g. reads the NAR
  cart contents) before re-executing; where not verifiable, the proposal
  goes to FAILED for a human eye rather than blind re-execution.
- A *legitimately new* run for the same week (Zach requeues after a fix)
  passes an explicit `attempt_salt`, visibly changing the key — duplicates
  are impossible by accident and deliberate by design.
