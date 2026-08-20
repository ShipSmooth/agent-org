# Agents

## v1 has exactly one agent: Replenishment

There is no Chief of Staff agent and no Deputy agents — **deliberately**.
Zach performs those roles; the approval gate is the editor. Coordination,
prioritization, and judgment about the business stay human. The plumbing
(tasks, broker, policy, tenancy) is built so more agents can be added
later, but v1 ships one.

An "agent" here is a plain Python class (no framework) that runs a task:
deterministic code does everything numeric; a model call, routed through
the single `LLMClient` abstraction, is used only for language work.
**Model routing:** cheap fast models for mechanical work (parsing supplier
pages, formatting, classification); capable models for anything that
computes a quantity or triggers an action — selectable per task in config.
In practice the replenishment quantities are pure arithmetic and use no
model at all; the model writes the report prose.

## Replenishment Agent

**Entity:** iThrive Medical. **Schedule:** weekly (Mon 06:00, config).

**Scope:** compute the weekly reorder per docs/replenishment.md; split by
supplier per docs/supplier-model.md; propose (a) staging the NAR cart and
(b) the report email, both Tier 2, through the ActionBroker.

**Tools** (each a broker action or a read client, nothing else):

- `veeqo.read_inventory`, `veeqo.read_orders`, `veeqo.read_fba_inbound` (Tier 0)
- `shopify.read_products` (Tier 0, BOM cross-check only — Shopify stock
  numbers are placeholders and are never read as inventory)
- `internal.*` state writes (Tier 1)
- `nar.stage_cart` (Tier 2), `notify.email` (Tier 2), `notify.sms`
  (Tier 2, urgent/anomalous only)

**Tiers it can reach:** 0–2 by rule; its proposals escalate to Tier 3 on
the anomaly triggers in docs/policy.md.

**It escalates (to Zach, via the report or SMS) rather than deciding:**

- Any anomaly trigger firing (Tier 3 path).
- A kit selling with no BOM entry; a component with unknown supplier;
  Veeqo/config BOM disagreement.
- Data-source failure after retries (run fails loudly).
- NAR freight quotes (discovered at checkout, reported, never accepted).
- Any build recommendation — assembly labour is human-planned in v1.

**"Done" for a weekly run:** all reads succeeded and validated; every
sellable SKU classified and computed; NAR draft PO staged after approval
(with freight quote captured) or approval denied/expired; gap list
persisted; report email delivered; every step in the audit log; task
SUCCEEDED. A run that ends FAILED with a clear notification is an
acceptable outcome; a run that guesses is not.

**It must never:**

- Purchase anything, or accept a freight quote (no `purchase` capability
  exists in v1).
- Call any integration except through the ActionBroker.
- Use Shopify inventory quantities as stock, or any cached/stale value for
  on-hand.
- Order from, or stage anything at, report-only suppliers (Dynarex, Amazon
  Business, own packaging) — gap list only.
- Invent a quantity with the model: every number traces to the documented
  arithmetic.
- Act for any entity other than the one on its task (RLS makes this fail
  anyway).
- Retry a Tier 2/3 action after denial, or re-execute an EXPIRED approval
  without fresh numbers.
- Modify BOMs, policy, parameters, or its own configuration.
