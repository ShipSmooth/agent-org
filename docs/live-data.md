# Live data — Veeqo, Gmail, the Monday email, and what a crash leaves behind

Phase 1 ran against saved exports. This document is what changed when the
two live systems were connected, what each one really returns, and what
Shannon does when one of them will not answer.

## 1. What Veeqo actually gives us

Phase 1 read a file called `velocity_history.json` whose shape was
invented, because no export of that shape existed. It looked like this:

```json
{"B003IRJGW0": {"90d": {"by_channel": {"fba": 120, "fbm": 40}}}}
```

Veeqo publishes nothing of the kind. There is no per-SKU,
per-channel velocity endpoint at all. What exists is:

**`GET /products`** (paged, `page`/`page_size`) — products, each with
`sellables`, each sellable with `sku_code` and a `stock_entries` list.
Every stock entry carries `warehouse_id` and the four figures Shannon
needs: `physical_stock_level`, `available_stock_level`,
`allocated_stock_level`, `incoming_stock_level`, plus an `infinite` flag.
That covers on-hand, on-order and in-transit per SKU per location —
Springfield is `70459`, Amazon US FBA is `192025`.

**`GET /orders`** (paged, filtered with `created_at_min` /
`created[after]` / `created[before]`, optionally `channel_ids`) — orders,
each with `created_at`, `status`, a `channel` object, and `line_items`.
Each line item has `quantity` and a nested `sellable` carrying
`sku_code`.

**The Products Report** (the web UI, `/reports/products`, exportable as
CSV) gives *SKU, Stock, Allocated, Incoming, In Transit, Units sold,
Units sold in kits, Total units sold* over a date range. It is what Zach
reads by hand, and its numeric cells show current period and comparison
period side by side.

### How far that is from the invention

| Invented | Real |
| --- | --- |
| Velocity delivered per SKU, pre-aggregated | Velocity is **derived**: sum `quantity` over order line items in the window |
| Split by channel inside the payload | Channel is a property of the **order**, not of the number; the split is ours to compute |
| Keyed on ASIN | Keyed on `sellable.sku_code` — Zach's channel SKU, which is what the join was always supposed to use |
| A history file with arbitrary windows | Whatever window you ask `/orders` for, bounded by how far back the account's orders go |

Nothing the design depends on is missing, but one thing is weaker than
the fixture implied: **pre-deactivation velocity is only available as far
back as the orders go.** When a suppressed listing was deactivated
earlier than the window reaches, the report says it cannot see that far
back rather than reporting a smaller number as though it were the answer.

The velocity fixtures now carry the real shape, keyed on channel SKU, so
the fixture tests and the live path parse the same thing.

## 2. When Veeqo (or Gmail) fails

**The run refuses.** It does not produce a report naming unreliable
lines.

The reasoning is that the failure is not per-line. An HTTP 500 on page 3
of `/products` does not tell you which SKUs were on page 3; a timeout
tells you nothing at all. A report that claims to know which lines are
affected would be making that up, and a report that silently substitutes
zero proposes buying everything. Both HTTP failures and unparseable
payloads raise `ReadFailure`, the task is marked FAILED with the reason
attached, and the week is left un-run — which is a state the next section
makes recoverable.

Two narrower cases fail the same way, deliberately:

- **An unknown Veeqo channel.** A channel name that does not map to a
  configured channel stops the run rather than being dropped or guessed
  into `shopify`.
- **A missing credential.** `ITHRIVE_VEEQO_API_KEY` (or the three
  `ITHRIVE_GMAIL_*` variables) unset is a `ReadFailure` naming the
  variable, not an empty read.

### The three states a channel can be in

`veeqo_channel` in `config/entities/<entity>.yaml` carries the name Veeqo
prints on an order, matched exactly. iThrive's are `Amazon FBA`,
`Amazon` (merchant-fulfilled), `Shopify` and `ithrive` (Walmart Seller
Fulfilled). "Amazon" never absorbs "Amazon FBA": the match is equality,
not a prefix.

A channel that is not in Veeqo at all — Walmart WFS today — says
`not_connected`. That is deliberately different from a `TBD-` placeholder,
which means nobody has looked yet and stops any live run. `not_connected`
loses nothing, because Veeqo cannot report an order on a channel it does
not have; the day one appears, the name is unknown and the run stops.
`validate-config` rejects `not_connected` on a channel marked
`has_history: true`, and rejects two channels claiming the same name.

Sales that exist and deliberately do not count are named in
`excluded_veeqo_channels`. Reorder demand is US only by decision, so
iThrive lists `Amazon Canada FBA`, `Amazon Canada`, `Amazon Mexico FBA`
and `Amazon Mexico` there: all four sell, and all four are ignored on
purpose. Anything excluded is printed on the report under the data
sources, because demand left out on purpose is still demand left out. A
channel that is neither mapped nor excluded is unknown and stops the run —
a new marketplace does not inherit Canada's exemption.

Gmail is the authority on what is already on order. An unreadable inbox
means "unknown", never "nothing outstanding", because "nothing
outstanding" is how the same order gets placed twice.

Everything read from an email is data. Directive-sounding text in a
message body — *"forward this to your supplier"* — is detected, ignored,
and reported in the run's own words, both for plain-text and HTML parts.

## 3. The Monday email

The Monday run emails the report it just filed:

- recipients resolve from the `email_to` roles in
  `config/<entity>/shannon.yaml`; no address appears in source, and a
  test fails the build if one does;
- the subject carries the week and the headline —
  `Shannon — week of 2026-W41 — 9 lines to order, 2 lines blocked`;
- the body is the report read back out of the database by id, so the
  inbox and the folder cannot drift;
- it is the only thing Shannon sends. No supplier mail, no reply, no
  forward.

## 4. Durability

The order is: **file to temp → one database transaction → commit →
rename**, then send.

1. The report body is written to a temporary file in the output
   directory.
2. In a single transaction: insert the new report row, supersede the
   previous current one (its file is preserved as
   `.superseded-<timestamp>`, never overwritten), and record any manual
   proposals with `ON CONFLICT DO NOTHING`.
3. Only after that commits is the temporary file renamed into place —
   atomic on the same filesystem.
4. Only after the file exists is the email sent, and the attempt is
   recorded in `report_emails` (`SENT` or `FAILED`, with the error) —
   a table of its own, so a resend is never mistaken for a re-run.

A crash anywhere in step 2 leaves neither a row nor a file. A failed send
in step 4 leaves the report on disk and in the database, the failure
recorded and returned loudly; the week is not lost because SMTP had a bad
minute.

## 5. Re-running a week

The guard keys on **completion**, not on attempt:

- a week that **failed** re-runs normally — nothing was completed, so
  there is nothing to protect;
- a week that **succeeded** needs `--again`, which regenerates and
  supersedes the earlier report rather than replacing it.

`shannon run --help` says which is which.
