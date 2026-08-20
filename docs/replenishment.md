# Replenishment calculation — full specification

This is the heart of the system. A different engineer must be able to
implement the calculator from this document alone. Where the design makes a
judgment call, the call and its reasoning are stated inline.

All quantities are integers of sellable units. All time is in weeks. All
parameters marked *(param)* are configurable per entity (and, where noted,
per component or per channel) — none are constants in code.

---

## 1. The three product types

| Type | Example | Reorder math |
|---|---|---|
| **NAR finished kits** — bought complete from NAR, resold (mostly FBM, some FBA) | NAR Bleeding Control Kit | Forecast directly as a purchasable SKU. **No explosion.** |
| **HMZ kits** — assembled in-house from components | HMZ IFAK (4 colourways), Essential (Mobile, Wall Mounted), Express, Basic C-A-T, Basic C-A-T Red Bag, Basic SAM XT | **Never purchased.** Demand is exploded through the BOM into component demand (§2). The kit itself gets a *build* recommendation, not a purchase. |
| **NAR components sold standalone** (mostly FBA, some FBM) | 30-0001 CAT Black, 10-0042 HyFin Vent Compact Twin | Forecast directly as a purchasable SKU. No explosion. A single physical component may receive demand from **both** standalone sales and kit explosion; the two are summed (§3). |

Type assignment is explicit in the product record (`products.product_type`),
seeded by rule and hand-checked: SKUs matching the NAR pattern `NN-NNNN` are
NAR items; active Shopify products with empty SKUs are Zach's own (HMZ). The
Shopify `vendor` field reads "iThrive" on all 81 products and is ignored.
Archived Shopify products are excluded entirely — they sell on no channel.

## 2. BOM explosion

**Source of truth for BOMs is a version-controlled config file, not Veeqo.**
Reason: Veeqo does not support bundles for FBA. HMZ kits sold via FBA exist
in Veeqo as separate simple products with **no link** to the components they
consume. Veeqo's FBM bundles do carry BOMs, but rather than merge two
sources of truth (config for FBA, Veeqo for FBM) we hold the entire BOM in
config and use Veeqo BOMs only as a weekly cross-check: if a Veeqo FBM
bundle disagrees with config, the run flags a data-quality warning and
config wins. One source of truth, drift made visible.

### 2.1 BOM config format (`config/<entity>/boms.yaml`)

```yaml
bom_version: 2026-08-19          # date-stamped; printed on every report
kits:
  HMZ-IFAK-BLK:
    aliases:                     # every SKU that represents this kit
      fba: HMZ-IFAK-BLK-FBA     # the Veeqo "simple product" used for FBA stock
      fbm: HMZ-IFAK-BLK          # the Veeqo bundle SKU used for FBM/Shopify
    components:
      - sku: 30-0001             # CAT Tourniquet, Black
        qty: 1
      - sku: 10-0042             # HyFin Vent Compact, Twin
        qty: 1
      - sku: PKG-IFAK-POUCH-BLK  # own packaging: MOLLE pouch
        qty: 1
      - sku: CONS-GLOVE-NTR-L    # consumable: nitrile gloves (pair)
        qty: 2
      # ... remaining lines
```

Rules:

- Every HMZ kit MUST appear here, with **all** component lines including
  packaging and consumables (55 lines across the six kits today).
- `aliases` maps the kit to every channel-specific SKU. Sales of any alias
  are demand for the same kit. This is the explicit fix for the Veeqo FBA
  limitation: the FBA simple product is joined to its recipe here and only
  here.
- No nested kits (Veeqo caps bundles at 25 components, no nesting; we adopt
  the same limits so config and Veeqo stay comparable).
- A kit selling anywhere with no BOM entry is a **hard run failure**, not a
  warning — silently un-exploded demand is exactly the bug this design
  exists to prevent.

### 2.2 Explosion

For component `c`, kit `k`, channel `ch`:

```
kit_demand(c) = Σ_k Σ_ch bom_qty(c, k) × forecast(k, ch)
```

where `forecast(k, ch)` sums sales across all of k's aliases on that channel.

## 3. The full demand equation (forecast regime)

For each purchasable component or NAR finished kit `c`:

```
horizon H          = lead_time_weeks(c) + cover_target_weeks(c)        (param, param)
standalone(c)      = Σ_ch velocity(c, ch) × H
from_kits(c)       = Σ_k Σ_ch bom_qty(c, k) × velocity(k, ch) × H
safety_stock(c)    = safety_stock_weeks(c) × ( Σ_ch velocity(c, ch)
                                             + Σ_k Σ_ch bom_qty(c,k) × velocity(k,ch) )   (param)

gross_demand(c)    = standalone(c) + from_kits(c) + safety_stock(c)

net_requirement(c) = gross_demand(c)
                     − on_hand(c)          # Veeqo, all warehouses + FBA sellable
                     − on_order(c)         # our own open POs (order_history)
                     − in_transit(c)       # Veeqo FBA inbound + supplier ASNs

order_qty(c)       = 0                        if net_requirement(c) ≤ 0
                   = moq_round(net_requirement(c), c)   otherwise        (§6)
```

Definitions and rules:

- `velocity(x, ch)` = units sold per week on channel `ch`, trailing
  `velocity_window_weeks` *(param, default 8)*, from **Veeqo orders only**.
  Shopify inventory numbers are placeholders (999/2000/1) and are never
  read as stock or velocity.
- Channels are data, not code: Amazon FBA, Amazon FBM, Shopify are
  populated; Walmart Seller Fulfilled and Walmart WFS exist in config with
  zero velocity until they have history. The sums above are over N
  channels; nothing breaks when a channel's velocity is zero.
- `on_hand` never includes FBA *reserved/unfulfillable* stock.
- All three subtraction terms come from live reads at run time; a failed
  read fails the run (see docs/architecture.md failure model). We never
  substitute a cached value for stock.

**Known weakness, stated plainly:** trailing-average velocity has no
seasonality and no trend. For trauma supplies with fairly steady demand this
is acceptable for v1, and the safety-stock weeks are the honest buffer. If
demand is seasonal, this under- or over-orders; the fix is a better
`velocity()` later, and nothing else in the equation changes.

## 4. Two regimes, and how a component is assigned one

| | **Forecast** | **Reorder point** |
|---|---|---|
| Applies to | NAR components and NAR finished kits | Consumables: gloves, tape, markers, own packaging, Dynarex/Amazon Business lines |
| Math | Full equation in §3 | `flag if available(c) < reorder_point(c)`, where `available = on_hand + on_order + in_transit`; suggested top-up = `reorder_target(c) − available(c)` |
| Output | Purchase quantity, MOQ-rounded | A line on the gap list. No quantity math beyond the top-up suggestion. |
| Why | Being off by 200 tourniquets is a real problem | Being off by 200 pairs of gloves costs nothing; do not over-engineer |

Assignment is an explicit field on the component record
(`components.regime`, values `forecast` | `reorder_point`), seeded by rule —
`supplier = NAR → forecast`, everything else → `reorder_point` — and
overridable per component in config. `reorder_point` and `reorder_target`
are per-component *(param)*.

## 5. Multi-supplier resolution

Every component carries a `supplier` field (NAR, Dynarex, Amazon Business,
own-packaging, unresolved — ~5 lines are unresolved today and are treated as
`reorder_point` gap-list lines flagged "supplier unknown"). After the demand
math, output splits by supplier **capability** (see docs/supplier-model.md):

- **NAR lines → a draft purchase order** the agent can act on: staged as a
  cart on narescue.com (Tier 2, approval required). Freight is LTL and
  auto-quoted only at checkout — the agent captures and reports the quote;
  it never predicts freight and never accepts it.
- **All other lines → the gap list**, a report-only section of the weekly
  email: component, supplier, available, threshold, suggested top-up. The
  agent may not order these in v1; Zach (or Justin) places those orders by
  hand.

The split is driven by data, never hardcoded: if Dynarex gains an ordering
integration later, its capability changes in config and its lines move from
gap list to actionable without touching the calculator.

## 6. MOQ rounding

Each NAR component may carry `moq_min` and `moq_increment` *(param, per
component)*:

- CAT tourniquets: `moq_min = 400`, `moq_increment = 200`
- HyFin chest seals: `moq_min = 750`, `moq_increment = 150`

**Rounding rule — always round UP:**

```
moq_round(q, c):
    if q ≤ 0:            return 0
    if q ≤ moq_min:      return moq_min
    else:                return moq_min + ceil((q − moq_min) / moq_increment) × moq_increment
```

Components with no MOQ configured use `moq_min = 0, moq_increment = 1`
(i.e. no rounding).

So for CAT: demand 410 → **600**, not 400. Demand 400 → 400. Demand 401 →
600. Judgment call, stated: rounding to *nearest* (410 → 400) can silently
leave you 10 tourniquets short of computed need on a safety-critical item;
rounding up trades a bounded amount of extra inventory (at most one
increment) for never under-covering. If the extra cash tied up ever matters,
the honest fix is lowering `cover_target_weeks`, not fudging the rounding.
The report always shows both the raw `net_requirement` and the rounded
order quantity so the rounding cost is visible per line.

## 7. Channel allocation

Given limited on-hand stock of a sellable SKU, allocation answers "how much
do we send to FBA, reserve for Walmart, and keep for merchant-fulfilled?"

```
allocatable(c)   = on_hand(c) − mf_floor(c)
mf_floor(c)      = mf_floor_weeks(c) × Σ_{ch ∈ merchant-fulfilled} velocity(c, ch)   (param)

fba_target(c)    = fba_cover_weeks(c) × velocity(c, FBA)                             (param)
fba_send(c)      = clamp( fba_target(c) − fba_on_hand(c) − fba_inbound(c),
                          0, allocatable(c) )

walmart_reserve(c) = walmart_reserve_units(c)          (param, default 0 — zero history,
                                                        cannot be velocity-based yet)
remainder stays merchant-fulfilled at the warehouse.
```

Priority order when stock cannot satisfy all three: **merchant-fulfilled
floor first** (Justin must be able to ship tomorrow's FBM/Shopify orders),
then FBA, then Walmart reserve. Judgment call: FBA is ~the revenue engine,
but an FBM stockout is an immediate defect on live orders, so the floor
wins. The floor defaults to 2 weeks *(param)*.

## 8. FBA inbound planning — constraint satisfaction

Amazon requires 5–10 boxes per shipment, every box packed identically.
Given per-SKU targets `fba_send(c)` for the SKUs in a shipment:

Find box count `B ∈ [box_min, box_max]` *(params, defaults 5, 10)* and
per-SKU per-box quantities `Q(c) ≥ 0` (integers, at least one nonzero,
subject to per-box weight/volume caps *(param)* when configured) minimizing:

```
error = Σ_c | B × Q(c) − fba_send(c) |     with the constraint B × Q(c) ≤ fba_send(c) + overship_tolerance(c)
```

Search is brute force: B has 6 values, Q(c) = round-down and round-to-
nearest of `fba_send(c)/B` per SKU — at most a few dozen candidates. No
solver library; this problem is tiny and exhaustive search is verifiable.
Ties break toward larger B (smaller, lighter boxes). The plan output shows
per SKU: target, planned `B × Q(c)`, and the shortfall/overage, so the
approximation is never hidden. Example: target 240 of one SKU → B = 8,
Q = 30, exact. Target 250 → B = 5, Q = 50, exact. Target 253 → best is
B = 6..10 giving 250 (B=5,Q=50); ships 3 short, shown on the plan.

## 9. Configurable parameters (summary)

Per entity, overridable per component/channel where noted:
`velocity_window_weeks` (8), `cover_target_weeks` (per component, 8),
`lead_time_weeks` (per supplier, NAR default 2), `safety_stock_weeks` (per
component, 2), `moq_min`/`moq_increment` (per component), `reorder_point`/
`reorder_target` (per consumable), `mf_floor_weeks` (2), `fba_cover_weeks`
(8), `walmart_reserve_units` (0), `box_min`/`box_max` (5/10),
`overship_tolerance` (0). Live in `config/<entity>/replenishment.yaml`;
every run's report prints the parameter values it used.

## 10. Worked example — three real SKUs, end to end

Parameters: velocity window 8 wk; NAR lead time 2 wk; cover target 8 wk →
**H = 10 wk**; safety stock 2 wk; CAT MOQ 400/200; HyFin MOQ 750/150.

SKUs: **HMZ-IFAK-BLK** (HMZ kit), **30-0001** CAT Tourniquet Black,
**10-0042** HyFin Vent Compact Twin. BOM: each IFAK contains 1 × 30-0001
and 1 × 10-0042 (plus packaging/consumable lines omitted here — they go to
the gap list).

**Step 1 — kit velocity (Veeqo, trailing 8 wk):**
HMZ-IFAK-BLK: FBA 30/wk (via alias HMZ-IFAK-BLK-FBA) + FBM 5/wk +
Shopify 2/wk = **37/wk**. Kit forecast over H: 37 × 10 = **370 kits**.
Explosion → 370 × 1 = **370** each of 30-0001 and 10-0042.

**Step 2 — 30-0001 CAT Black:**
- standalone velocity: FBA 40 + FBM 8 + Shopify 2 = 50/wk → 50 × 10 = 500
- from kits: 370
- safety stock: 2 × (50 + 37) = 174
- gross = 500 + 370 + 174 = **1,044**
- minus on_hand 350, on_order 200, in_transit 100 → net = 1,044 − 650 = **394**
- MOQ round: 394 ≤ 400 → **order 400** (extra 6 shown on report)

**Step 3 — 10-0042 HyFin Twin:**
- standalone velocity: FBA 30 + FBM 5 = 35/wk → 350
- from kits: 370
- safety stock: 2 × (35 + 37) = 144
- gross = 350 + 370 + 144 = **864**
- minus on_hand 54, on_order 0, in_transit 0 → net = **810**
- MOQ round: 810 > 750 → 750 + ceil((810−750)/150) × 150 = 750 + 150 = **order 900**

**Step 4 — the kit itself:** HMZ-IFAK-BLK is never purchased. Build
recommendation = kit gross demand minus assembled stock: gross = 370 +
safety 2 × 37 = 74 → 444; on_hand assembled 120 (warehouse 40 + FBA 80),
FBA inbound 0 → **build 324 kits** (assembly-labour note attached; not
modelled in v1).

**Step 5 — channel allocation for the kit:** warehouse on_hand 40; MF floor
= 2 × (5 + 2) = 14 → allocatable 26. FBA target = 8 × 30 = 240; FBA
on-hand 80, inbound 0 → want 160, clamped to **send 26 now**; the remaining
134 come from the 324-kit build. Walmart reserve 0 (no history).

**Step 6 — FBA inbound plan** (after build, sending target 240): B ∈ [5,10],
Q = 240/B → **B = 8 boxes, Q = 30 per box**, 8 × 30 = 240 exact.

**Step 7 — supplier split:** 30-0001 (400) and 10-0042 (900) → NAR draft PO,
staged as a narescue.com cart on approval, freight quote captured at
checkout and reported. IFAK packaging and gloves → gap list (reorder-point
check), report-only.
