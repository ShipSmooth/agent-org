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
| **Dynarex** | None — Zach orders on dynarex.com himself, from his own account | `read_catalog`, `report_only` | Krinkle gauze 3161, petrolatum gauze 3553, Sensi-Wrap 3173, cold packs 3683. Cart staging was retired on 9 Sep 2026 when the site began serving an image CAPTCHA, which nobody is to automate past. Each component carries the `product_url` of its exact item code, so the report links Zach straight to the page. Lead time 1 week (~5 business days). |
| **Amazon Business** | None — no account access, and the cart-URL plan was dropped (9 Sep 2026) | `report_only` | Overlaps Dynarex lines; whichever supplier a component record names. The part number *is* the purchase ASIN, so the report links each line to `amazon.com/dp/<ASIN>` and Zach adds it himself. |
| **World Richman** (soft goods: carriers, pouches, bags) | None | `report_only` | Part numbers follow `<kit sku>-bag`. Lead time 9 weeks (~60 days), the longest in the system, so this supplier carries its own `cover_target_weeks: 13`. |
| **Own printed** (instruction cards) | None | `report_only` | Lead time 2 weeks. Four cards, both printers confirmed: `CARD-ESSENTIAL-EXPRESS` (20-314, 20-315, 25-001, 25-002) and the two Basic cards `CARD-BASIC-CAT` (25-010) and `CARD-BASIC-SAMXT` (26-002) from Next Day Flyers; `CARD-REDBAG` (26-001) from 48HourPrint. |
| **SAM Medical** (SAM XT tourniquets) | None | `report_only` | Bought direct, not through NAR. Lead time 2 weeks. |
| **Orca Tactical Gear** (Coyote/Multicam pouches) | None | `report_only` | Replaces World Richman for those two colourways, whose MOQ made them unviable. Lead time 2 weeks (~10 calendar days). Orca publishes **no item numbers at all**: `ORCA-MOLLE-EMT-COYOTE` and `ORCA-MOLLE-EMT-MULTICAM` are our own references, flagged `part_is_internal_reference: true`, and ordering quotes the product name. |
| **internal** (state) | — | none | Real stock held loose, no supplier attached yet. Reports and prompts only; never a cart, never a run failure. Today: the wall mount only. The triangular bandage left this state on 21 Aug 2026 for Dynarex `3681`. |
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

## When the supplier has no part numbers

A component's identity is `(supplier, supplier_part_number)`, and Orca
breaks it: they publish nothing to key on. Such a component carries
`part_is_internal_reference: true`, which says the part number is **ours**
and means nothing to the supplier. `validate-config` then requires a
non-empty `name`, because the name is the only thing that can go on a
purchase order, and the report leads with the product name and labels the
reference as ours — otherwise somebody quotes a SKU Orca has never heard
of, on Shannon's authority. This is the one narrow exception to "never
invent an identifier": the supplier genuinely has none.

## How capability constrains Shannon

The replenishment output is split per supplier (docs/replenishment.md §5):

- Lines whose supplier has `stage_cart` — NAR alone — → an ActionProposal
  to stage that supplier's cart, Tier 2 (see docs/policy.md). Cart
  quantities are always **purchase units** (docs/replenishment.md §6.1),
  never sellable units.
- Lines whose supplier is `report_only` → the report's ORDER THESE BY HAND
  section, one block per supplier, each line carrying its quantity, its
  item code and — where one can be proved to name that exact item — a link
  to its product page. No per-line action exists for Shannon to take.
- `ops_consumable` components never enter this split at all — they belong
  to the separate calendar-triggered reminder (docs/replenishment.md §4.1),
  which shares only the cart-staging machinery.

Enforcement is layered: (1) the calculator only *generates* actionable lines
for capable suppliers; (2) the ActionBroker independently re-checks
capability on every proposal, so a calculator bug cannot smuggle a Dynarex
order through; (3) the audit log records the capability check outcome;
(4) the client that talks to the supplier has no method, path or HTTP verb
that could check out, pay or place an order — see docs/cart-staging.md.

## Adding or upgrading a supplier

Grant a capability by editing the supplier record/config — e.g. if World
Richman ever exposes ordering, add `stage_cart` and implement its broker
executor. The calculator, policy engine, and report format do not change. Downgrading
(revoking a capability) takes effect on the next proposal immediately.
