# Replenishment calculation — full specification

This is the heart of Shannon, the replenishment agent (see
docs/conventions.md for the naming convention). A different engineer must be able to
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
| **HMZ kits** — assembled in-house from components | Seven kits: Essential (Mobile, Wall Mounted), Express, Basic C-A-T, Basic C-A-T Red Bag, Basic SAM XT, IFAK with CAT Gen 7 & HyFin (4 colourways: IFAK-CAT-BLACK/-GREEN/-COYOTE/-MULTICAM; 13 BOM lines), Compact IFAK Trauma Kit (6 BOM lines; different carrier from the full IFAK) | **Never purchased.** Demand is exploded through the BOM into component demand (§2). The kit itself gets a *build* recommendation, not a purchase. |
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
  IFAK-CAT-BLACK:                       # live SKU; other colourways: IFAK-CAT-GREEN,
    aliases:                            # IFAK-CAT-COYOTE, IFAK-CAT-MULTICAM
      fba: TBD-1                        # the Veeqo "simple product" used for FBA stock
      fbm: IFAK-CAT-BLACK               # the Veeqo bundle SKU used for FBM/Shopify
    components:                         # identity is (supplier, supplier_part_number)
      - supplier: nar
        part: 30-0001                   # C-A-T tourniquet Gen 7, black
        qty: 1
      - supplier: nar
        part: 10-0042                   # HyFin Vent Compact, twin pack
        qty: 1
      - supplier: dynarex
        part: "3161"                    # sterile krinkle gauze
        qty: 1
      - supplier: own_packaging
        part: TBD-2                     # carrier pouch
        qty: 1
      # ... remaining lines (13 total for this kit)
```

Rules:

- Every HMZ kit MUST appear here, with **all** component lines including
  packaging, consumables, and non-stocked lines (61 lines across the seven
  kits today).
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

## 3. The full demand equation (forecast class)

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
  read as stock or velocity; Shopify is a source of product descriptions
  and BOM cross-checks only. Order history likewise comes from Veeqo (it
  aggregates the channels); Amazon Seller Central is a cross-check, never
  the primary.
- Demand always originates on the **sales** side: a component's standalone
  velocity is read via the product listing that carries its `sales_asin`
  (or channel SKU). A `purchase_asin` is an acquisition path and never
  creates demand (§5).
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

## 4. Component classes — the class decides whether it can be bought at all

Every component carries a **required** `class` enum that selects the code
path. There is no default: a component with no class is a configuration
error that **fails loudly at config load** — it must never fall through to
a purchasing path.

| Class | What it is | Treatment |
|---|---|---|
| `forecast` | NAR and other high-value kit components | Demand forecast from channel velocity, exploded through kit BOMs (§3). MOQ and increment rules apply (§6). Enters the staged NAR cart. |
| `reorder_point` | Kit consumables — gloves, markers, tape, gauze, packaging | Stock level against a threshold: `flag if available(c) < reorder_point(c)` where `available = on_hand + on_order + in_transit`; suggested top-up = `reorder_target(c) − available(c)`. No forecast. Enters a supplier PO or an Amazon Business cart. |
| `non_stocked` | In a BOM, but never held in inventory (first case: a wall mount used in the Essential Wall Mounted kit) | **Excluded from every purchase calculation.** Present so the kit is described correctly and kit cost is right, but never placed in a cart, a PO, or a recommendation. Purchase quantity is always 0. Treated as infinite supply for build-feasibility. |
| `ops_consumable` | Shipping and warehouse supplies — tape, boxes, mailers, thermal labels, void fill | Never counted, never forecast, never explodes from a BOM. Time-triggered reminder only (§4.1). |

Why the class is mandatory: a model that assumes every BOM line is
purchasable produces a wrong recommendation on the Essential Wall Mounted
kit's first order (it would try to buy wall mounts Zach does not stock).
`reorder_point`/`reorder_target` are per-component *(param)*. Rationale for
two purchasing regimes: being off by 200 tourniquets is a real problem;
being off by 200 pairs of gloves costs nothing — do not over-engineer the
consumables.

### 4.1 Ops consumables — a reminder path with no inventory behind it

Ops consumables are in no kit BOM, not tracked in Veeqo, not counted
anywhere. Shannon has no data to forecast them and must not pretend
otherwise. This is **not** modelled as inventory with a null stock level —
that would leak "unknown quantity" into downstream calculations. It is a
separate calendar trigger with its own report type, sharing only the
cart-staging machinery with the forecast path:

- Fires on a configurable cadence *(param, default 6 weeks)*.
- Emits a report listing every `ops_consumable` with its `purchase_asin`.
- Offers to stage an Amazon Business cart built from those ASINs.
- Tiers: the report/draft is **Tier 0** (it neither spends nor reaches an
  outside party); staging the cart is **Tier 1**, notify after.
- Recipient list is configurable per component group *(param)* — the
  fulfilment lead receives this one, not only Zach.

```yaml
# config/<entity>/shannon.yaml (excerpt)
ops_reminders:
  cadence_weeks: 6
  groups:
    shipping_supplies:
      recipients: [zach, fulfilment_lead]
      components:                    # class must be ops_consumable
        - {supplier: amazon_business, part: TBD-ASIN-1, name: "2in shipping tape"}
        - {supplier: amazon_business, part: TBD-ASIN-2, name: "12x10x8 boxes"}
```

## 5. Component identity, Amazon identifiers, and multi-supplier resolution

**Component identity is `(supplier, supplier_part_number)`.** A NAR part
number (`30-0001`), a Dynarex item number (`3161`), and an Amazon ASIN
(`B00006IFHD`) are all valid `supplier_part_number` values. The `supplier`
field is **required** and selects the acquisition path — narescue.com
browser automation, a supplier purchase order, or an Amazon Business cart
URL. `supplier: pending` is an explicit state, never a default: a `pending`
component fails loudly at config load if its class would route it to any
purchase path, and appears on the gap list flagged "supplier pending". One
line is pending today: the Latex Tourniquet Band (open between NAR BOA
30-0009/30-0071 and Dynarex 3139). Do not build a NAR-shaped model and bolt
other suppliers on: fewer than half the kit BOM lines (23 of 61) are NAR.

**Amazon identifiers point in two directions and cannot share a field:**

- `purchase_asin` — a component bought on Amazon Business. An **input**;
  builds a staged purchase cart. Lives on the **component** record beside
  `supplier` and `supplier_part_number`.
- `sales_asin` — a finished product listed on Amazon, sold FBA or FBM. An
  **output**; read for velocity, which is what creates component demand.
  Lives on the **product/listing** record.

All three cases are representable: a kit has a `sales_asin` and no
`purchase_asin` (assembled, not bought); a marker has a `purchase_asin` and
no `sales_asin`; a NAR component resold standalone has **both** — roughly
75 NAR SKUs are in this case. The same physical object is bought as a
component and sold as a product: two identifiers describing two different
relationships, coexisting (component record + linked product record), never
de-duplicated into one `asin` field. A single field breaks case 3 quietly —
velocity gets attributed to the wrong side and the reorder quantity is
wrong with no error raised.

After the demand math, output splits by supplier **capability** (see
docs/supplier-model.md):

- **NAR lines → a draft purchase order** Shannon can act on: staged as a
  cart on narescue.com (Tier 2, approval required). Freight is LTL and
  auto-quoted only at checkout — she captures and reports the quote,
  never predicts it and never accepts it.
- **Amazon Business `reorder_point` lines →** the gap list, plus an offered
  Amazon Business cart built from `purchase_asin`s (Tier 1 to stage — a
  cart URL touches no account and spends nothing).
- **All other lines → the gap list**, report-only: component, supplier,
  available, threshold, suggested top-up. Zach (or Justin) orders by hand.

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
`overship_tolerance` (0), `ops_reminders.cadence_weeks` (6) and recipient
lists. Live in `config/<entity>/shannon.yaml`; every run's report prints
the parameter values it used.

## 10. Worked example — real SKUs, end to end

The hand-checkable prose version, spanning all three component classes,
lives in docs/plain-english-overview.md — that is the copy Zach verifies.
This section keeps the same numbers in spec form; the two must never
disagree.

Parameters: velocity window 8 wk; NAR lead time 2 wk; cover target 8 wk →
**H = 10 wk**; safety stock 2 wk; CAT MOQ 400/200; HyFin MOQ 750/150.

SKUs: the four IFAK colourways (**IFAK-CAT-BLACK/-GREEN/-COYOTE/
-MULTICAM**, HMZ kits), **30-0001** C-A-T Gen 7 Black (class `forecast`;
carries both a `sales_asin` on its standalone listing and a
`purchase_asin`; only the sales side drives demand), **10-0042** HyFin
Vent Compact Twin (class `forecast`), **Dynarex 3161** sterile krinkle
gauze (class `reorder_point`), and the Essential Wall Mounted **wall
mount** (class `non_stocked`, supplier part TBD-3). BOM: each IFAK
colourway contains 1 × 30-0001 and 1 × 10-0042 (other lines go to the gap
list or are non-stocked).

**Step 1 — kit velocity (Veeqo, trailing 8 wk):**
the four IFAK colourways combined: FBA 30/wk (via their FBA alias simple
products) + FBM 5/wk + Shopify 2/wk = **37/wk**. Kit forecast over H:
37 × 10 = **370 kits**. Explosion → 370 × 1 = **370** each of 30-0001 and
10-0042.

**Step 2 — 30-0001 C-A-T Black (forecast; demand read via `sales_asin`):**
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

**Step 4 — Dynarex 3161 gauze (reorder_point):** available = on_hand 180 +
on_order 60 + in_transit 0 = 240; `reorder_point` 300, `reorder_target`
600. 240 < 300 → gap-list line: top-up 600 − 240 = **360**, with its
Dynarex item number and `purchase_asin`. No forecast, no MOQ math, no cart
Shannon can act on.

**Step 5 — wall mount (non_stocked):** appears in the Essential Wall
Mounted BOM for description and cost only. Purchase quantity **0**, always;
treated as infinite supply for build feasibility. If it were misclassified
as `forecast`, the kit's first order would recommend buying it — the
mandatory class exists to make that impossible.

**Step 6 — the kit itself:** an IFAK colourway is never purchased. Build
recommendation = kit gross demand minus assembled stock: gross = 370 +
safety 2 × 37 = 74 → 444; on_hand assembled 120 (warehouse 40 + FBA 80),
FBA inbound 0 → **build 324 kits** (assembly-labour note attached; not
modelled in v1).

**Step 7 — channel allocation for the kit:** warehouse on_hand 40; MF floor
= 2 × (5 + 2) = 14 → allocatable 26. FBA target = 8 × 30 = 240; FBA
on-hand 80, inbound 0 → want 160, clamped to **send 26 now**; the remaining
134 come from the 324-kit build. Walmart reserve 0 (no history).

**Step 8 — FBA inbound plan** (after build, sending target 240): B ∈ [5,10],
Q = 240/B → **B = 8 boxes, Q = 30 per box**, 8 × 30 = 240 exact.

**Step 9 — supplier split:** 30-0001 (400) and 10-0042 (900) → NAR draft PO,
staged as a narescue.com cart on approval, freight quote captured at
checkout and reported. Dynarex 3161 (360) and other consumables → gap list,
with an offered Amazon Business cart where a `purchase_asin` exists. The
wall mount → nowhere, by class.
