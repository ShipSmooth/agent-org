# Staging a supplier's cart

Shannon reads the week she already reported and puts those lines in the
supplier's cart. She does not check out. That is not a setting, and this
document is mostly about why it cannot become one.

    uv run shannon stage                     # dry run: reads the cart, changes nothing
    uv run shannon stage --live              # refused while max_tier_this_phase is 0
    uv run shannon stage --week 2026-W35     # a particular week
    uv run shannon stage --fixtures ''       # read the real cart rather than a saved copy

## It acts on the report, and calculates nothing

`shannon stage` reads the live `replenishment` report row for the week and
takes the lines that report routes to `<supplier>_cart` — the routing the
calculator already worked out from the supplier's capability. Nothing is
recomputed. If the numbers are wrong the fix is `shannon run --again`,
which produces a new report; staging then follows it.

Two conversions come off that row rather than being redone:

- the quantity is `purchase_units`, so a component sold in cases of 25
  goes in the cart as 4, not 100 (docs/replenishment.md §6.1);
- a line whose part number is *ours* — the supplier publishes none — has
  no SKU to add, so it is left out of the cart and named in the report as
  one to order by hand.

## Dry run is the default

Without `--live`, Shannon reads the supplier's cart, works out exactly
which SKUs and quantities would be added, writes the confirmation report
and emails it. The supplier's cart is never written to. The action is
`nar.plan_cart_staging`, Tier 0, and it is the only staging action this
phase can run.

`--live` submits `nar.stage_cart`, which is Tier 2 in
`config/policy/global.yaml` and refused by the broker while
`max_tier_this_phase` is 0. Raising that ceiling is a deliberate,
reviewable change to policy — and it still cannot buy anything.

Raising the ceiling to 2 would also switch on every other Tier 2 action —
SMS, supplier mail — which is not what "let Shannon fill the NAR cart"
means. So policy takes `phase_exceptions`: one action, by name, allowed
above the ceiling and no further than the tier written beside it.

    phase_exceptions:
      - action: nar.stage_cart
        up_to_tier: 2

The list is empty today, so live staging is refused. An exception must
name an action that already has a rule, and must say how far it goes;
neither can be left out.

## A configurable's variant comes from the catalogue, never the page

80-0167 is not a product to post: it is the "Gauze (no Hemostatic)"
variant of the configurable parent 80-0168-s, and its two siblings are
different kits at roughly twice the price. So before adding a line
Shannon reads `/graphql` for the SKU and gets back either a simple
product — added as itself — or the parent plus the option value that
selects that exact child, which is what goes in the payload.

She refuses rather than guesses: an unknown SKU, a parent posted without a
variant, a child the parent does not list, a missing option id or value
index, or a catalogue that answers with anything unexpected all raise
`CartRefusal` and stage nothing. Magento answers the add with the child it
resolved, and that answer is compared with the child that was asked for;
a mismatch, or a quantity that is not the one requested, fails the line
loudly rather than leaving something unexplained in the cart.

## What a live run checks afterwards

A line is not called added because the site returned 200. After live
staging Shannon reads the cart again and, per line, checks that it now
holds what it held before plus what was added. She also checks that
nothing that was in the cart beforehand has gone or shrunk — she cannot
remove a line, so if one disappears the site did it, and the report says
so in as many words. Either check failing produces a CHECK THE CART
YOURSELF line at the top of the report; nothing is retried and nothing is
reordered.

## Never checking out is code, not configuration

Four independent things have to be true before a purchase could happen,
and each is somewhere different:

1. **No supplier has the `purchase` capability.** The broker checks
   capability before policy, so an action needing it is refused before a
   tier is even consulted.
2. **The client has no method that buys.** `SupplierCart` (in
   `agent_org.integrations.carts`) can read a cart and add a line. There
   is no checkout, no payment and no submit on the interface, so there is
   nothing for a caller — or a model — to reach for.
3. **The paths are allow-listed.** `NarCartClient` may request exactly
   five URLs — a login, the cart, its totals, its items and the
   catalogue. Anything else raises `CartRefusal` before a socket opens,
   and a second check refuses any path containing a checkout, order,
   payment or billing word even if someone adds it to the list later.
4. **The methods are allow-listed.** Only GET and POST are ever sent.
   Magento places an order with `PUT /rest/V1/carts/mine/order` and empties
   a cart with DELETE; both are refused by method as well as by path.

Tiers can be raised. None of the four above can be, without a code change
that shows up in a diff as exactly what it is.

## Doing it twice

A cart line cannot be taken back out — Shannon has no capability to remove
one — so staging has to be safe to repeat. Two guards, at different
depths:

- The **broker** fingerprints the action; a second identical run is
  recognised as the same action and hands back the first one's answer
  without executing again.
- The **ledger** (`cart_stagings`, migration 0009) records one row per
  SKU per week per supplier per mode, with a unique key. The executor
  consults it per line, so a crashed run retried with a payload the broker
  sees as new still cannot add the same SKU twice. It reports the line as
  already staged instead.

A dry run is recorded under mode `DRY_RUN` and therefore never suppresses
the live staging that follows it: a rehearsal that cancelled the
performance would be a trap.

## What is already in the cart

Read first, reported in full, and left alone. Zach puts things in that
cart himself, and the confirmation report shows the cart before the run
and after it, with totals, so the two can be compared against the browser.

## The confirmation report

Same shape as the weekly report — a summary at the top, detail below —
written to the reports folder, stored in the database and emailed to the
owner role of the entity, exactly like the weekly one and through the same
executor. It shows what was added (or would be), the quantity, what the
cart says it recorded, anything that failed and why, anything that cannot
be staged by SKU, both cart states, and the line this whole design exists
to let her write honestly:

> NOTHING WAS SUBMITTED. No checkout was started, no payment details were
> entered, and no order was placed.

## Credentials

`NAR_EMAIL` and `NAR_PASSWORD`, under the entity's own prefix, read from
the environment at the moment of use. Never in source, never in a report,
never in a log — a refused login reports its status code and not its body.
A missing or refused login stops the run; it never becomes an empty cart.

## Suppliers still to come

Dynarex next (portal form, no API), Amazon Business last, and its Cart API
gets its own action name rather than inheriting the Tier 1 rule for the
URL-only staging that exists today.
