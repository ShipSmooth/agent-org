# Agents

## v1 has exactly one agent: Shannon

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

## Shannon

Shannon, the replenishment agent, handles inventory replenishment for
iThrive Medical. She uses she/her; see docs/conventions.md for the naming
convention. Identifier in code, config, logs, metrics, and the database:
`shannon`. She signs the emails and SMS she sends as Shannon.

**Entity:** iThrive Medical. **Schedule:** weekly (Mon 06:00, config),
plus the calendar-triggered ops-consumable reminder every 6 weeks (config).

**Scope:** compute the weekly reorder per docs/replenishment.md; split by
supplier per docs/supplier-model.md; propose (a) staging the NAR cart and
(b) the report email, both Tier 2, through the ActionBroker. Separately:
emit the ops-consumable reminder report (Tier 0) on its cadence and offer
to stage the Amazon Business cart from its purchase ASINs (Tier 1, notify
after). Everything she sends goes to Zach alone, to and from
zach@ithrivemedical.com — resolved from the `zach` role in
`config/ithrive/shannon.yaml`, never a hard-coded address. His ShipSmooth
address is a vendor/tooling identity and receives no agent mail.

**Tools** (each a broker action or a read client, nothing else):

- `veeqo.read_inventory`, `veeqo.read_orders`, `veeqo.read_fba_inbound` (Tier 0)
- `shopify.read_products` (Tier 0, BOM cross-check only — Shopify stock
  numbers are placeholders and are never read as inventory)
- `gmail.read_order_signals` (Tier 0 — the authoritative `on_order`
  source; a read failure or ambiguous signal fails the run, exactly like
  a Veeqo failure. The narescue.com order-status field is never read.)
- `nar.read_order_history` (Tier 0 — order numbers and per-line
  quantities only, never the status field)
- `internal.*` state writes (Tier 1)
- `amazon_business.stage_cart` (Tier 1, ops-consumable cart URL)
- `nar.stage_cart` (Tier 2), `dynarex.stage_cart` (Tier 2), `notify.email`
  (Tier 2), `notify.sms` (Tier 2, urgent/anomalous only)

**Tiers she can reach:** 0–2 by rule; her proposals escalate to Tier 3 on
the anomaly triggers in docs/policy.md.

**She escalates (to Zach, via the report or SMS) rather than deciding:**

- Any anomaly trigger firing (Tier 3 path).
- A kit selling with no BOM entry; a component with a pending supplier or
  (impossible past config load) missing class; Veeqo/config BOM
  disagreement; a pack-size mismatch against the confirmed value (that
  line halts and is flagged).
- Low stock on an `internal` or `unsourced` component — she prompts,
  never picks a supplier.
- Gmail unavailable or ambiguous on outstanding NAR orders — she asks
  which orders are still awaiting shipment; she never guesses.
- Data-source failure after retries (run fails loudly).
- NAR freight quotes (discovered at checkout, reported, never accepted).
- Any build recommendation — assembly labour is human-planned in v1.

**"Done" for a weekly run:** `shannon validate-config` passed (plain-
English errors, non-zero exit on bad config — see docs/replenishment.md
§13); all reads succeeded and validated; every
sellable SKU classified and computed; NAR draft PO staged after approval
(with freight quote captured) or approval denied/expired; gap list
persisted; the parking lot (docs/replenishment.md §12) carried in the
report with ages; report email delivered; every step in the audit log;
task SUCCEEDED. A run that ends FAILED with a clear notification is an
acceptable outcome; a run that guesses is not.

**She must never:**

- Purchase anything, or accept a freight quote (no `purchase` capability
  exists in v1).
- Call any integration except through the ActionBroker.
- Use Shopify inventory quantities as stock, or any cached/stale value for
  on-hand.
- Check out, pay, or confirm an order anywhere — at NAR or Dynarex she
  stages the cart and stops, always.
- Order from, or stage anything at, report-only suppliers (World Richman,
  own printed) — gap list only. (Amazon Business cart *URLs* are the one
  staging she may do below Tier 2, and they spend nothing.)
- Read the narescue.com order-status field, or guess at outstanding
  orders when Gmail is unavailable.
- Put sellable-unit quantities in a cart — cart quantities are purchase
  units, always (docs/replenishment.md §6.1).
- Remove a parking-lot item — only Zach clears them.
- Purchase, forecast, or count a `non_stocked` or `ops_consumable`
  component: non-stocked lines always resolve to purchase quantity 0;
  ops consumables exist only on the calendar-triggered reminder.
- Invent a quantity with the model: every number traces to the documented
  arithmetic.
- Act for any entity other than the one on her task (RLS makes this fail
  anyway).
- Retry a Tier 2/3 action after denial, or re-execute an EXPIRED approval
  without fresh numbers.
- Modify BOMs, policy, parameters, or her own configuration.
