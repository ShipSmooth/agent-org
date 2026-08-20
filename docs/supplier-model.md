# Supplier model

Suppliers are data, not code paths. Each supplier record declares
**capabilities**, and a supplier's capabilities are the hard ceiling on what
the agent may even *propose* for that supplier's lines. The ActionBroker
rejects any proposal that exceeds the supplier's declared capability —
before policy tiers are even consulted.

## Suppliers in v1 (iThrive)

| Supplier | Integration | Capabilities | Notes |
|---|---|---|---|
| **NAR** (North American Rescue) | Browser automation (headless Chromium) against narescue.com — **no API**, confirmed with vendor | `read_catalog`, `read_order_history`, `stage_cart` | Session expires frequently and requires clicking a login button; automation re-logs-in from env-var credentials (Chrome saved passwords are unreachable from a container). Freight is LTL, auto-quoted only at checkout: discovered and reported, never predicted. Catalogue updates arrive monthly, manually, from Zach's NAR contact. **No `purchase` capability in v1.** |
| **Dynarex** | None | `report_only` | Gloves, Israeli bandages, emergency blankets, tape, markers. |
| **Amazon Business** | None | `report_only` | Overlaps Dynarex lines; whichever supplier a component record names. |
| **Own packaging** (Zach's print/pack vendors) | None | `report_only` | Instruction cards, carrying cases, resealable bags, MOLLE pouches. |
| *(unresolved, ~5 lines)* | — | `report_only` | Flagged "supplier unknown" on every gap list until assigned. |

## Capability vocabulary

- `read_catalog` — read prices/availability.
- `read_order_history` — read past orders.
- `stage_cart` — assemble a cart/draft order **without** purchasing.
- `purchase` — commit money. **No supplier has this in v1.** The tier
  mechanism for it exists (Tier 2 minimum, Tier 3 on anomaly), but no
  supplier record grants it, so a purchase proposal is rejected at the
  capability check regardless of tier.
- `report_only` — the null capability: lines appear on the gap list only.

## How capability constrains the agent

The replenishment output is split per supplier (docs/replenishment.md §5):

- Lines whose supplier has `stage_cart` → an ActionProposal to stage that
  supplier's cart (Tier 2). Today that is NAR only.
- Lines whose supplier is `report_only` → gap-list entries inside the weekly
  report proposal. No per-line action exists for the agent to take.

Enforcement is layered: (1) the calculator only *generates* actionable lines
for capable suppliers; (2) the ActionBroker independently re-checks
capability on every proposal, so a calculator bug cannot smuggle a Dynarex
order through; (3) the audit log records the capability check outcome.

## Adding or upgrading a supplier

Grant a capability by editing the supplier record/config — e.g. if Dynarex
ever exposes ordering, add `stage_cart` and implement its broker executor.
The calculator, policy engine, and report format do not change. Downgrading
(revoking a capability) takes effect on the next proposal immediately.
