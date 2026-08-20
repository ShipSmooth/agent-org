# Policy — tiers, declarative YAML, default-deny

Policy is data, not code. The policy engine loads a global default file plus
per-entity overrides, resolves the tier for every ActionProposal, and the
broker enforces the outcome. Changing a threshold is a config PR, never a
code change.

## The four tiers

| Tier | Meaning | Gate |
|---|---|---|
| 0 | Silent | Reads, analysis, internal drafts. No broker involvement. |
| 1 | Notify after | Executes immediately; Zach is told in the next digest. |
| 2 | Approval | Waits for Zach's approval (email link; SMS if urgent). |
| 3 | Approval + second confirmation | Two distinct confirmations, the second restating totals and anomaly reasons. |

**Default-deny:** any action matching no rule resolves to Tier 3. Unknown
means maximum caution.

## Tier 3 anomaly triggers

A Tier 2 purchase-related action is **escalated to Tier 3** if ANY of:

1. order total > `$75,000` (`absolute_total_usd`)
2. order total > 150% of the trailing 8-order average total
3. any line quantity > 2× that line's trailing average quantity
4. total units > 2× the trailing average total units

Plus categorically Tier 3: payroll, contracts, credential changes, and any
action registered irreversible. Trailing averages come from
`order_history`; with fewer than `min_history_orders` *(default 4)* past
orders the comparative triggers cannot fire meaningfully, so any purchase
action is Tier 3 until history exists — conservative by construction.

## YAML format

`config/policy/global.yaml` (defaults) and `config/<entity>/policy.yaml`
(overrides). Per-entity files inherit global values and may only override
`thresholds:` and add entries to `rules:`; they cannot delete global rules
or lower a global rule's tier below its global value.

```yaml
# config/policy/global.yaml
version: 1
default_tier: 3                     # DEFAULT DENY

thresholds:
  tier3_escalation:
    absolute_total_usd: 75000
    total_vs_trailing_avg_pct: 150   # trailing 8 orders
    line_qty_vs_trailing_avg_x: 2.0
    total_units_vs_trailing_avg_x: 2.0
    trailing_window_orders: 8
    min_history_orders: 4
  approval_ttl_days: 7

rules:
  # ---- Tier 0: silent reads and internal work ----
  - action: veeqo.read_inventory          # weekly stock/velocity pull
    tier: 0
  - action: shopify.read_products         # BOM cross-check read
    tier: 0
  - action: internal.write_draft_report   # drafting the reorder report
    tier: 0

  # ---- Tier 1: internal state, notify after ----
  - action: internal.enqueue_task              # scheduling a follow-up run
    tier: 1
  - action: internal.record_gap_list           # persisting the consumables gap list
    tier: 1
  - action: internal.update_forecast_params    # writing computed velocities to DB
    tier: 1

  # ---- Tier 2: reaches outside the company / any purchase action ----
  - action: nar.stage_cart                # stage the weekly NAR order cart
    tier: 2
  - action: notify.email                  # send Zach the reorder report
    tier: 2
  - action: fba.create_inbound_plan       # create an FBA inbound shipment plan
    tier: 2
  - action: shopify.update_product        # e.g. correcting a listing's contents
    tier: 2

  # ---- Tier 3: categorical ----
  - action: nar.purchase                  # checkout at NAR (capability not granted in v1)
    tier: 3
  - action: shopify.bulk_price_update     # storewide price change — hard to fully undo
    tier: 3
  - action: credentials.rotate            # any credential change
    tier: 3

escalations:                              # applied on top of a rule's tier
  - match: {category: purchase}
    to_tier: 3
    when_any:
      - total_usd_gt: ${thresholds.tier3_escalation.absolute_total_usd}
      - total_vs_trailing_avg_pct_gt: ${thresholds.tier3_escalation.total_vs_trailing_avg_pct}
      - line_qty_vs_trailing_avg_x_gt: ${thresholds.tier3_escalation.line_qty_vs_trailing_avg_x}
      - total_units_vs_trailing_avg_x_gt: ${thresholds.tier3_escalation.total_units_vs_trailing_avg_x}
  - match: {reversible: false}
    to_tier: 3
```

```yaml
# config/ithrive/policy.yaml — per-entity override, inherits global
version: 1
thresholds:
  tier3_escalation:
    absolute_total_usd: 75000     # explicit even when equal to global — self-documenting
# a typical NAR order is $15k–$65k, so the $75k line is a real anomaly bar
```

## Concrete examples, resolved

| Proposed action | Resolution |
|---|---|
| Stage the weekly NAR cart, total $42,300 (typical) | Tier 2 — approval email. |
| Stage a NAR cart totaling $81,000 | Tier 2 rule + trigger 1 ($75k) → **Tier 3**, approve then confirm. |
| Stage a NAR cart with 1,300 CATs when the trailing line average is 500 | Trigger 3 (2.6×) → **Tier 3**. |
| Create an FBA inbound plan, 8 boxes of IFAKs | Tier 2; approval notes it becomes irreversible after carrier pickup. |
| Email Zach the weekly report | Tier 2 (reaches a person; he is the approver, so in practice it is the approval itself). |
| Update a Shopify product description | Tier 2. |
| Write computed velocities to the database | Tier 1, in the next digest. |
| Read Veeqo inventory | Tier 0, silent. |
| Anything not matching a rule (e.g. a future agent invents `qbo.create_bill`) | **Tier 3 by default-deny.** |

## Evaluation order

capability check (supplier-model) → rule match (else `default_tier`) →
escalations → highest tier wins. The resolved tier, the matched rule, and
every fired trigger are stored on the proposal and printed in the approval
message, so Zach always sees *why* something needs a second confirmation.
