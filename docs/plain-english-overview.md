# The Org, explained in plain English

This document explains the whole system without jargon. Every technical term
used anywhere else in these docs is defined here first. If anything here is
unclear, the design is wrong, not you.

## What we are building

A piece of software that runs on the Dell OptiPlex in your home office. Its
first job in version 1: once a week, figure out what iThrive Medical needs
to reorder, stage that order in a cart on narescue.com, and send you a
report. It never spends money. You review, you click buy (or don't).

The worker is **Shannon, the replenishment agent** — a program that uses an
AI language model for judgment calls (like writing the summary email) and
plain arithmetic for anything involving quantities or money. The AI never
invents a number. Shannon is the only agent in v1. No "Chief of Staff", no
deputies — you are those roles, and your approval is the gate. Every week
she:

- reads your inventory and sales numbers,
- does the reorder arithmetic and shows her work,
- fills a shopping cart at NAR but never presses "checkout",
- writes down everything she did in a permanent logbook,
- and asks permission before doing anything that touches the outside world.

## The other pieces

**The Action Broker** is the single doorway. Anything Shannon wants to do
with a side effect — stage a cart, send an email — goes through it as a
written proposal, checked against the rulebook and logged before it runs.
There is no back door, and the automated build checks fail if anyone writes
code that tries one.

**The Policy Engine** is the rulebook. Every action has a tier: **Tier 0**
silent (reads, drafts); **Tier 1** do it, then tell you; **Tier 2** ask
first (anything reaching outside the company, any purchase action);
**Tier 3** ask, then confirm a second time (orders over $75,000 or far
above your recent averages, payroll, contracts, anything irreversible).
Anything matching no rule is treated as Tier 3. Approvals expire if unseen —
Tier 2 after 72 hours, Tier 3 after 7 days, each with a halfway reminder —
and an expired approval is a "no" that gets re-raised with fresh numbers,
never a silent "yes". SMS (with a one-time code, never a bare "Y") can
approve Tier 2 and below; Tier 3 always requires email plus the second
confirmation. Both work without the VPN.

**The database** (Postgres — standard, boring, reliable) holds products,
suppliers, parts lists, proposals, approvals, and the logbook. Every row is
stamped with which LLC it belongs to, and the database itself refuses
cross-company reads. Adding a fourth LLC is a configuration file, not a
programming project.

## Where the numbers come from

**Veeqo** is the truth for stock, sales velocity, and inbound shipments —
including order history. **Shopify** is used only for product descriptions
and parts lists; its stock numbers are placeholders (999, 2000, 1) and are
never trusted. **narescue.com** has no API, so Shannon drives a real web
browser to log in and stage the cart, as you would by hand.

## Component classes — what can even be bought

Every part carries a required class; a part without one fails loudly at
startup rather than sneaking into a purchase path:

- **forecast** — NAR and other high-value kit components. Full demand math,
  minimum-order rounding, into the staged NAR cart.
- **reorder_point** — kit consumables (gloves, tape, gauze, packaging).
  Flagged when stock drops below a threshold; into a supplier PO or an
  Amazon Business cart.
- **non_stocked** — in a kit's parts list but never held in inventory (the
  first is a wall mount used in one kit variant). Counted for kit
  description and cost, **never** placed in any cart or recommendation.
- **ops_consumable** — shipping tape, boxes, mailers, labels, void fill.
  In no kit, tracked nowhere, so Shannon has no data and doesn't pretend
  otherwise: a calendar nudge every 6 weeks (configurable) lists each item
  with its Amazon purchase link and offers to stage a cart. The report is
  Tier 0; staging the cart is Tier 1. It goes to the fulfilment lead too,
  not only you.

Suppliers matter as much as classes: only 23 of the 61 kit component lines
come from NAR. Each part is identified by its supplier plus that supplier's
part number, and the supplier decides how it can be bought — NAR via the
browser, Dynarex via a PO, Amazon Business via a cart. A part whose supplier
is still `pending` (one remains: the latex tourniquet band) can only appear
on the report, never in a cart.

## How a weekly run flows

1. Monday 06:00, a schedule wakes Shannon.
2. She pulls stock, sales, and inbound data from Veeqo. If Veeqo is down,
   she stops and tells you; she never guesses.
3. She computes demand. The seven kits you assemble in-house are
   "exploded": forecast kit sales become component needs via the parts
   lists in configuration (Veeqo can't link FBA kits to their parts, so the
   recipes live in a config file you can read).
4. She splits the list by supplier: NAR lines become a draft order; other
   suppliers' lines become a gap list she can only report.
5. NAR lines are rounded **up** to NAR's minimums and case steps.
6. She proposes two actions — stage the NAR cart, email you the report —
   both Tier 2. An unusually large order escalates to Tier 3.
7. You approve from the email (no VPN needed). She stages the cart,
   captures NAR's freight quote (discoverable only at checkout), and
   attaches it.
8. You buy, or you don't. Her job ends at the staged cart.

## The worked example — check this by hand

Assumptions used throughout: sales velocity is the average of the last 8
weeks of Veeqo orders; NAR delivery takes 2 weeks; you want 8 weeks of
cover, so Shannon plans for 10 weeks (2 + 8); safety stock is 2 extra weeks
of sales as a cushion against a bad forecast.

**Line 1 — 30-0001, C-A-T tourniquet, black. Class: forecast.** This part
points in two directions: it has a *sales* ASIN (you sell it standalone on
Amazon; placeholder TBD-ASIN-1 until pulled from the live listing) and a
*purchase* ASIN (it can also be bought on Amazon Business; placeholder
TBD-ASIN-2).
Only the **sales** side drives demand — velocity comes from what customers
buy; the purchase ASIN is just a way to acquire it and creates no demand.
- Standalone sales: 50/week (40 FBA + 8 FBM + 2 Shopify). Over 10 weeks: **500**.
- Kit sales: the four IFAK colourways (IFAK-CAT-BLACK, -GREEN, -COYOTE,
  -MULTICAM) sell 37/week combined, each containing one black C-A-T.
  Over 10 weeks: **370**.
- Safety stock: 2 weeks × (50 + 37) = **174**.
- Raw requirement: 500 + 370 + 174 = **1,044**.
- You already have: 350 on the shelf, 200 on order at NAR, 100 in transit
  to Amazon = 650. Still needed: 1,044 − 650 = **394**.
- NAR's rule: minimum 400, then steps of 200. 394 is under the minimum, so
  **order 400**. (Had it been 410, Shannon orders **600** — she always
  rounds up, never down to 400, because 10 short on tourniquets is worse
  than 190 spare. The report shows raw and rounded side by side.) For the
  other rule: a HyFin chest-seal need of 810 becomes **900** — minimum 750,
  then steps of 150, so 750 → 900.

**Line 2 — Dynarex 3161, sterile krinkle gauze. Class: reorder_point.**
No forecast. On the shelf plus on order: 240; threshold 300 (your number,
in config). 240 < 300, so it goes on the gap list: "gauze low — top up to
600 (target) − 240 = **360**", with its Dynarex item number and Amazon
purchase link. Shannon cannot order it; she flags it.

**Line 3 — wall mount for the Essential (Wall Mounted) kit. Class:
non_stocked.** In the kit's parts list so the kit's contents and cost are
right, but you don't stock or reorder it. Purchase quantity: **0**, always.
For "can we build kits?" it is treated as never the limiting part. If this
class were missing, the kit's first order would recommend buying wall
mounts you don't want — that's why the class is mandatory.

## What happens when something breaks

Every task and proposal is logged **before** it runs and updated after, so
the logbook never claims less than what happened. Veeqo down → retry, then
stop and notify; never proceed on stale numbers. NAR login expires mid-run
(it does) → log back in and resume; if login fails, stop loudly. Machine
crashes → on restart, interrupted tasks are re-run safely; every proposal
has a fingerprint so a re-run cannot stage the same cart or email you
twice. The AI hangs or loops → time and step budgets kill the task and
report it. A failed run costs a week's convenience, never money.

## Honest limitations

- Forecasts are trailing averages: no seasonality, no trend. Safety stock
  is the buffer until real error data says otherwise.
- Walmart is wired in but silent until it has sales history.
- Kit assembly labour (Angie and Stephanie's hands) is noted, not modelled.
  "Build 324 kits" is arithmetic, not a staffing plan, in v1.
- The kit recipes live in a config file. If a recipe changes and the file
  isn't updated, the math is confidently wrong. Every report prints the
  recipe version so drift is visible, but keeping it current is a human
  duty.
