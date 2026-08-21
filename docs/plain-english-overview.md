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
browser to log in and stage the cart, as you would by hand — and its
order-status page lies (it shows delivered orders as "Processing"), so
Shannon never reads it. **Gmail** is the truth for what is still on order
from NAR: an order counts as outstanding only if there is a confirmation
email with no matching shipping notification. If Gmail can't be read or
the answer is ambiguous, she stops and asks you — double-ordering is the
most expensive mistake this system can make.

## Component classes — what can even be bought

Every part carries a required class; a part without one fails loudly at
startup rather than sneaking into a purchase path:

- **forecast** — NAR and other high-value kit components. Full demand math,
  minimum-order rounding, into the staged NAR cart.
- **reorder_point** — kit consumables (gloves, tape, gauze, packaging).
  Flagged when stock drops below a threshold; into a staged cart where the
  supplier supports one (Dynarex, Amazon Business), otherwise onto the
  report for you to order by hand.
- **non_stocked** — in a kit's parts list but never held in inventory (the
  first is a wall mount used in one kit variant). Counted for kit
  description and cost, **never** placed in any cart or recommendation.
- **ops_consumable** — shipping tape, boxes, mailers, labels, void fill.
  In no kit, tracked nowhere, so Shannon has no data and doesn't pretend
  otherwise: a calendar nudge every 6 weeks (configurable) lists each item
  with its Amazon purchase link and offers to stage a cart. The report is
  Tier 0; staging the cart is Tier 1. Like everything Shannon sends, it
  goes to you alone, at zach@ithrivemedical.com.

Suppliers matter as much as classes: fewer than half the kit component
lines come from NAR (the real parts list is committed at
`config/ithrive/boms.yaml` — 83 lines across seven kits). Each part is
identified by its supplier plus that supplier's part number, and the
supplier decides how it can be bought — NAR and Dynarex via the browser
(cart staged, never checked out), Amazon Business via a cart link, World
Richman and printed cards by hand from the report. Three "no supplier"
situations are kept distinct: **pending** (a mistake to fix — none exist
today), **internal** (you hold stock loose, like the ~2,000 triangular
bandages — Shannon reports, never orders), and **unsourced** (deliberately
open, like the nitrile gloves you buy from whoever is cheapest — Shannon
prompts you when stock is low and never picks a supplier for you).

One more thing every part carries: its **pack size**. You think in single
units; suppliers often sell packs. The NAR nasopharyngeal airway is the
clear case — each kit uses one, NAR sells a two-pack, so a need of 370
airways means **185** in the cart, never 370. Shannon does all her math in
single units and converts to packs as the very last step. She reads pack
sizes off the live listing the first time she meets a part and asks you to
confirm; if a listing's pack size later changes, she halts that line and
flags it instead of ordering.

## How a weekly run flows

1. Monday 06:00, a schedule wakes Shannon.
2. She pulls stock, sales, and inbound data from Veeqo. If Veeqo is down,
   she stops and tells you; she never guesses.
3. She computes demand. The seven kits you assemble in-house are
   "exploded": forecast kit sales become component needs via the parts
   lists in configuration (Veeqo can't link FBA kits to their parts, so the
   recipes live in a config file you can read).
4. She checks Gmail for NAR orders still awaiting shipment, so nothing is
   ordered twice.
5. She splits the list by supplier: NAR and Dynarex lines become staged
   carts (with your approval); other suppliers' lines become a gap list
   she can only report.
6. Quantities are rounded **up** to each supplier's minimums and case
   steps, then up to the nearest 5, then converted to packs.
7. She proposes the actions — stage the carts, email you the report —
   Tier 2. An unusually large order escalates to Tier 3.
8. You approve from the email (no VPN needed). She stages the cart,
   captures NAR's freight quote (discoverable only at checkout), and
   attaches it.
9. You buy, or you don't. Her job ends at the staged cart. Every report
   also carries the **parking lot** — a numbered list of open issues
   (unsourced pouches, missing lead times, and so on), each showing what
   it blocks and how long it has been open. Items leave the list only when
   you clear them. And before any run, a `shannon validate-config` check
   reads the config and fails in plain English (file and line, no
   stack traces) if anything is missing or malformed.

## The worked example — check this by hand

Assumptions used throughout (your documented buying process, now the
defaults): sales velocity is the trailing **90 days** of Veeqo sales,
converted to a weekly rate (units in 90 days ÷ 12.857); NAR delivery takes
**3 weeks**; you want **7 weeks of total cover, including the lead time**
(3 lead + 4 buffer), so Shannon plans for 7 weeks — not 7 on top of 3;
no separate safety stock (the buffer is inside the 7); every quantity is
rounded up to the nearest 5 after any minimum-order rule.

**The kits first.** The four full-IFAK colourways (IFAK-CAT-BLACK, -GREEN,
-COYOTE, -MULTICAM) sold 450 units in the last 90 days → **35/week**
combined. The Compact IFAK (IFAK-CAT-COMPACT) sold 90 → **7/week**. Over
7 weeks: **245** full IFAKs and **49** Compacts. **Both** kits contain one
black C-A-T and one HyFin twin pack — summing across every kit that uses a
part is the whole point; miss the Compact and both lines below are wrong.

**Line 1 — 30-0001, C-A-T tourniquet, black. Class: forecast.** This part
points in two directions: it has a *sales* ASIN (you sell it standalone on
Amazon) and a *purchase* ASIN (it can also be bought on Amazon Business).
Only the **sales** side drives demand — velocity comes from what customers
buy; the purchase ASIN is just a way to acquire it and creates no demand.
- Standalone sales: 540 units in 90 days → 42/week. Over 7 weeks: **294**.
- Kit sales: full IFAKs 245 × 1 = 245, **plus** Compacts 49 × 1 = 49,
  together **294**.
- Raw requirement: 294 + 294 = **588**.
- You already have: 100 on the shelf, 60 on order at NAR (one confirmation
  email in Gmail with no shipping notification), 0 in transit = 160.
  Still needed: 588 − 160 = **428**.
- NAR's rule: minimum 400, then steps of 200. 428 is over the minimum, so
  round up one step: **600**. Already a multiple of 5, and NAR sells
  singles, so **600 goes in the cart**. (The report shows raw and rounded
  side by side.)

**Line 2 — 10-0042, HyFin chest-seal twin pack. Class: forecast.**
Standalone 270 units in 90 days → 21/week → **147** over 7 weeks; kits add
245 + 49 = **294**; raw requirement **441**. Minus 54 on the shelf =
**387**. HyFin's rule is minimum 750, steps of 150 — 387 is under the
minimum, so **order 750**.

**Line 3 — ZZ-0034, nasopharyngeal airway. Class: forecast, and the pack
example.** You buy it, you don't resell it — purchase side only, no sales
ASIN, so no standalone demand. Only the full IFAK uses it: **245** needed.
You hold about 118 loose (assumed here for round numbers): 245 − 118 =
**127**, rounded up to the nearest 5 = **130 airways**. NAR sells it as a
**two-pack**, so the cart line is 130 ÷ 2 = **65 packs** (which is 130
airways — never 130 packs).

**Line 4 — Dynarex 3161, sterile krinkle gauze. Class: reorder_point.**
(It's in the Essential and Express kits — not the IFAK.) No forecast. On
the shelf plus on order: 80; threshold 100 (your number, in config).
80 < 100, so top up to the target: 400 − 80 = **320**. Dynarex supports
cart staging, so with your approval this becomes a staged dynarex.com
cart — staged, never checked out.

**Line 5 — wall mount for the Essential (Wall Mounted) kit. Class:
non_stocked.** In the kit's parts list so the kit's contents and cost are
right, but you don't stock or reorder it. Purchase quantity: **0**, always.
For "can we build kits?" it is treated as never the limiting part. If this
class were missing, the kit's first order would recommend buying wall
mounts you don't want — that's why the class is mandatory. (The build
check does name each kit's real limiting part — today the Coyote and
Multicam IFAKs are blocked by zero pouch stock, parking-lot item PL-1.)

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

- Forecasts are trailing 90-day averages: no seasonality, no trend — your
  process says demand is steady year-round, and the 4-week buffer inside
  the cover target is the cushion.
- Walmart is wired in but silent until it has sales history.
- Kit assembly labour (Angie and Stephanie's hands) is noted, not modelled.
  "Build 125 kits" is arithmetic, not a staffing plan, in v1.
- The kit recipes live in a config file. If a recipe changes and the file
  isn't updated, the math is confidently wrong. Every report prints the
  recipe version so drift is visible, but keeping it current is a human
  duty.
