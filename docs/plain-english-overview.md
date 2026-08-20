# The Org, explained in plain English

This document explains the whole system without jargon. Every technical term
used anywhere else in these docs is defined here first. It is under 1,500
words. If anything here is unclear, the design is wrong, not you.

## What we are building

A piece of software that runs on the Dell OptiPlex in your home office. Its
first and only job in version 1: once a week, figure out what iThrive Medical
needs to reorder, stage that order in a cart on narescue.com, and send you a
report. It never spends money. You review, you click buy (or don't).

Think of it as a very diligent junior operations employee who:

- reads your inventory and sales numbers every week,
- does the reorder arithmetic and shows their work,
- fills a shopping cart at NAR but never presses "checkout",
- writes down everything it did in a permanent logbook,
- and asks permission before doing anything that touches the outside world.

## The pieces

**The Replenishment Agent.** The worker. "Agent" here just means a program
that uses an AI language model for judgment calls (like writing the summary
email) and plain arithmetic for anything involving quantities or money. The
quantities are computed by ordinary, testable math — the AI never invents a
number. There is exactly one agent in v1. No "Chief of Staff", no deputies —
you are those roles, and your approval is the gate.

**The Action Broker.** The single doorway. Any time the agent wants to do
something with a side effect — stage a cart, send you an email, update
anything outside its own notes — it must file a written proposal through the
Broker. The Broker checks the rulebook, records the proposal in the logbook,
and only then executes (or waits for your approval). There is no back door:
the agent physically cannot call NAR or Shopify directly, and our automated
checks fail the build if anyone ever writes code that tries.

**The Policy Engine.** The rulebook. Every possible action has a tier:

- **Tier 0** — silent: reading data, doing math, writing internal drafts.
- **Tier 1** — do it, then tell you: internal bookkeeping, queueing work.
- **Tier 2** — ask first: anything reaching outside the company — every
  purchase-related action, every message to an outsider.
- **Tier 3** — ask, then confirm a second time: unusually large orders
  (over $75,000, or far above your recent averages), plus payroll,
  contracts, and anything that can't be undone.

If an action matches no rule, it is treated as Tier 3. Unknown means
maximum caution, never "probably fine".

**The database.** One Postgres database (a standard, boring, reliable
database) holds everything: products, suppliers, component lists, tasks,
proposals, approvals, and the logbook. Every row is stamped with which
company it belongs to (iThrive, Lima Zulu, ShipSmooth), and the database
itself — not just our code — refuses to show one company's rows to a query
made on behalf of another. Adding a fourth LLC is a configuration file, not
a programming project.

## Where the numbers come from

- **Veeqo** is the source of truth for stock levels, sales velocity, and
  what's already inbound to Amazon.
- **Shopify** is used only for product/component lists (which components go
  into which kit). Its stock numbers are placeholders and are never trusted.
- **narescue.com** has no API — no machine-readable connection. The agent
  drives a real web browser, invisibly, to log in and stage the cart, the
  same way you would by hand.

## How a weekly reorder actually flows

1. **Monday 06:00** — a scheduled task wakes the agent.
2. **Gather** — it pulls stock, sales, and inbound-shipment data from Veeqo.
   If Veeqo is unreachable, it stops and tells you; it never guesses.
3. **Calculate** — it works out demand for every product. Kits you assemble
   in-house (the HMZ line) are "exploded": a forecast of 370 IFAK sales
   becomes a need for 370 tourniquets, 370 chest seals, and so on, using the
   parts lists in configuration. It subtracts what you have, what's already
   on order, and what's in transit.
4. **Split by supplier** — every component is tagged with its supplier. NAR
   lines become a draft purchase order. Dynarex, Amazon Business, and
   your-own-packaging lines become a "gap list" — a report of what's low
   that the agent can flag but not act on.
5. **Round** — NAR lines are rounded up to NAR's minimums and case
   increments (e.g. tourniquets: at least 400, then in steps of 200).
6. **Propose** — the agent files two proposals with the Broker: "stage this
   cart at NAR" and "email Zach the report". Both are Tier 2, so nothing
   happens until you approve. If the order is unusually large, Tier 3
   triggers and you must confirm twice.
7. **You approve** — from the email (works anywhere, no VPN needed). The
   Broker then drives the browser, stages the cart, captures NAR's freight
   quote (which can only be discovered at checkout, never predicted), and
   attaches it to the report.
8. **You buy, or you don't.** The system's job ends at the staged cart and
   the report. In v1 it has no purchase authority at all.

## Where your approval enters

Twice, structurally. First, the tier system: every consequential action
pauses and waits for you. Second, this repository itself: the agent's
behavior is defined in documents and configuration you can read, and
changing them goes through a pull request you approve. The approval gate is
the editor, as you put it.

Approvals arrive by **email** (full detail, line by line, with reasoning)
and by **SMS** (urgent or anomalous only). Both work without Tailscale,
because you travel. Only the management dashboard sits behind the VPN.

## What happens when something breaks

Every task moves through explicit states (waiting, running, needs-approval,
done, failed) and every state change is logged **before** the action is
attempted, then updated after. So the logbook can never claim less than
what actually happened.

- **Veeqo or Shopify errors out** — the task retries a few times with
  pauses; if it still fails, it stops, marks itself failed, and notifies
  you. It never proceeds on stale or partial data.
- **The NAR login session expires mid-run** (it does, frequently) — the
  browser automation logs back in and resumes. If login itself fails, the
  task stops and reports; nothing half-staged is left silently.
- **The computer crashes mid-task** — on restart, the system finds tasks
  marked "running" that shouldn't be, and re-runs them safely. Every
  proposal carries a fingerprint, so a re-run cannot stage the same cart
  twice or email you twice.
- **The AI model hangs or loops** — every task has a time limit and a step
  budget. Exceed either and the task is killed, marked failed, and reported.
  A failed run costs you one week's convenience, never money.

The failure philosophy throughout: **stop loudly, never guess quietly.** A
missed report you'll notice; a wrong order you might not.

## What this is deliberately not

- Not an AI framework product. Plain Python code we fully control, so the
  approval and audit guarantees stay checkable.
- Not autonomous purchasing. v1 stages carts; it never buys.
- Not a forecasting moonshot. NAR items get simple velocity math with
  honest safety margins. Cheap consumables (gloves, tape, markers) get a
  bare "flag when below X" — being off by 200 pairs of gloves costs
  nothing, so we don't over-engineer them.
- Not multi-agent yet. The plumbing supports more agents later; v1 ships
  one.

## Honest limitations

- The first forecasts will be only as good as your Veeqo history. Expect to
  correct the early recommendations; the config makes corrections cheap.
- Walmart is wired in but produces nothing until it has sales history — you
  cannot forecast from zero data.
- Kit assembly labour (Angie and Stephanie's hands) is noted but not
  modelled in v1. A recommendation to build 400 kits is arithmetic, not a
  staffing plan. You are the check on that until v2.
- The HMZ-kit parts lists for FBA live in a config file because Veeqo
  cannot link FBA bundles to components. If a recipe changes and the config
  isn't updated, the math is wrong. The weekly report shows the BOM version
  used, so drift is visible, but keeping it current is a human duty — yours
  and ours.
