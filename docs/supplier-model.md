# Supplier model

Suppliers are data, not code paths. Each supplier record declares
**capabilities**, and a supplier's capabilities are the hard ceiling on what
Shannon may even *propose* for that supplier's lines. Component identity is
`(supplier, supplier_part_number)` — a NAR part number (`30-0001`), a
Dynarex item number (`3161`), and an Amazon ASIN (`B00006IFHD`) are all
valid part numbers; the supplier selects the acquisition path. The model is
not NAR-shaped: fewer than half the kit BOM lines are NAR (see the
committed `config/ithrive/boms.yaml` for the real counts). The
ActionBroker
rejects any proposal that exceeds the supplier's declared capability —
before policy tiers are even consulted.

## Suppliers in v1 (iThrive)

| Supplier | Integration | Capabilities | Notes |
|---|---|---|---|
| **NAR** (North American Rescue) | Browser automation (headless Chromium) against narescue.com — **no API**, confirmed with vendor | `read_catalog`, `read_order_history`, `stage_cart` | Session expires frequently and requires clicking a login button; automation re-logs-in from env-var credentials (Chrome saved passwords are unreachable from a container). Freight is LTL, auto-quoted only at checkout: discovered and reported, never predicted. Catalogue updates arrive monthly, manually, from Zach's NAR contact. `read_order_history` covers order **numbers and per-line quantities only** — the site's order-status field is unreliable and must never be read; outstanding orders come from Gmail (docs/replenishment.md §3.1). **No `purchase` capability in v1.** |
| **Dynarex** | Browser automation against dynarex.com — Zach orders there directly via his account | `read_catalog`, `stage_cart` | Krinkle gauze 3161, petrolatum gauze 3553, Sensi-Wrap 3173, cold packs 3683. Credentials from env vars, `DYNAREX_` prefix. Same hard rule as NAR: stage the cart, **never check out**. Lead time TODO — Zach to provide (parking-lot PL-7). |
| **Amazon Business** | Cart URL construction from `purchase_asin`s (no account access) | `stage_cart` (URL only), `report_only` lines otherwise | Overlaps Dynarex lines; whichever supplier a component record names. Staging an ops-consumable cart is Tier 1, notify after — a cart URL spends nothing and reaches no outside party. |
| **World Richman** (soft goods: carriers, pouches, bags) | None | `report_only` | Part numbers follow `<kit sku>-bag`. Lead time TODO (PL-7). |
| **Own printed** (instruction cards) | None | `report_only` | Three card versions exist; kit mapping unresolved (PL-4). |
| **internal** (state) | — | none | Real stock held loose, no supplier attached yet. Reports and prompts only; never a cart, never a run failure. Today: triangular bandage `HMZ-0001` (~2,000 loose). |
| **unsourced** (state) | — | none | Deliberately open, permanently — Zach buys from whoever is cheapest. Shannon prompts when stock is low and **never picks a supplier for him**. Today: black nitrile gloves. |
| **pending** (state, 0 lines today) | — | `pending` | Not a default: config load fails loudly if a `pending` component's class routes to any purchase path; otherwise the line appears on the gap list flagged "supplier pending". The Latex Tourniquet Band was the last pending line; it was removed from every kit. |

## Capability vocabulary

- `read_catalog` — read prices/availability.
- `read_order_history` — read past orders.
- `stage_cart` — assemble a cart/draft order **without** purchasing.
- `purchase` — commit money. **No supplier has this in v1.** The tier
  mechanism for it exists (Tier 2 minimum, Tier 3 on anomaly), but no
  supplier record grants it, so a purchase proposal is rejected at the
  capability check regardless of tier.
- `report_only` — the null capability: lines appear on the gap list only.
- `pending` — an explicit unresolved-supplier state that fails loudly at
  config load rather than defaulting to anything. Distinct from `internal`
  (stock held, no supplier yet — prompts only, valid) and `unsourced`
  (deliberately no supplier, permanently — prompts only, valid). Conflating
  the three either blocks runs that should proceed or hides gaps.

## How capability constrains Shannon

The replenishment output is split per supplier (docs/replenishment.md §5):

- Lines whose supplier has `stage_cart` → an ActionProposal to stage that
  supplier's cart. NAR and Dynarex cart staging are Tier 2; an Amazon
  Business cart URL built from `purchase_asin`s is Tier 1 (see
  docs/policy.md). Cart quantities are always **purchase units**
  (docs/replenishment.md §6.1), never sellable units.
- Lines whose supplier is `report_only` → gap-list entries inside the weekly
  report proposal. No per-line action exists for Shannon to take.
- `ops_consumable` components never enter this split at all — they belong
  to the separate calendar-triggered reminder (docs/replenishment.md §4.1),
  which shares only the cart-staging machinery.

Enforcement is layered: (1) the calculator only *generates* actionable lines
for capable suppliers; (2) the ActionBroker independently re-checks
capability on every proposal, so a calculator bug cannot smuggle a Dynarex
order through; (3) the audit log records the capability check outcome.

## Adding or upgrading a supplier

Grant a capability by editing the supplier record/config — e.g. if World
Richman ever exposes ordering, add `stage_cart` and implement its broker
executor. The calculator, policy engine, and report format do not change. Downgrading
(revoking a capability) takes effect on the next proposal immediately.
