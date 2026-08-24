# FIRST-RUN.md — running Shannon for the first time

This is written for whoever sits at the Dell, not for a programmer. Follow
it top to bottom. Nothing in it can order anything, send anything or change
anything outside this machine — Phase 1 gives Shannon eyes and a
calculator, and nothing else.

Words used here:

- **Shannon** — the software that works out what iThrive needs to reorder
  and writes it up. She is a program, she has a name, and she is referred
  to by it.
- **Phase 1** — this stage of the work: Shannon reads, calculates, and
  writes a report. She cannot buy, email, text, or browse.
- **Terminal** — the black window where you type commands. On Windows,
  open **PowerShell** from the Start menu.
- **Repository** (repo) — the folder containing all of this software.
- **Database** — Postgres, where Shannon keeps her records. It runs on
  this machine, in Docker.
- **Docker** — software that runs the database in a sealed box, so you
  never install or configure Postgres by hand.
- **WSL2** — the Windows feature Docker needs in order to run Linux
  software on Windows.
- **Fixture** — a saved copy of a real export (a Veeqo spreadsheet, a set
  of emails). Shannon reads these instead of the live accounts. Phase 1
  reads *only* fixtures.
- **BOM** — bill of materials: the parts list for a kit.

---

## 1. What to install, in this order

Do these in order. Installing Docker Desktop before WSL2 makes Docker fail
in a way that is hard to diagnose.

1. **WSL2.** Open PowerShell as Administrator (right-click → *Run as
   administrator*) and run:

   ```powershell
   wsl --install
   ```

   Restart Windows when it asks. After the restart, check it worked:

   ```powershell
   wsl --status
   ```

   It should say the default version is 2.

2. **Docker Desktop.** Download from https://www.docker.com/products/docker-desktop/
   and install with the default options, which include *Use WSL 2 based
   engine*. Start Docker Desktop and leave it running; the whale icon in
   the system tray must not say "starting".

3. **Git.** Download from https://git-scm.com/download/win, default
   options. Check:

   ```powershell
   git --version
   ```

4. **Python 3.12 and uv.** `uv` installs and manages Python itself, so
   install `uv` and let it handle the rest:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   Close and reopen PowerShell, then check:

   ```powershell
   uv --version
   ```

5. **Get the repository.**

   ```powershell
   cd $HOME
   git clone https://github.com/ShipSmooth/agent-org.git
   cd agent-org
   uv sync
   ```

   `uv sync` downloads Python 3.12 and every library, at the exact
   versions recorded in the repository. It takes a few minutes the first
   time.

---

## 2. Credentials

**Phase 1 needs two passwords, both of which you invent yourself.** They
are for the database on this machine. Shannon does not connect to Veeqo,
Gmail, Amazon, Shopify or anything else in this phase — she reads saved
exports. Every other credential in `.env.example` belongs to a later
phase; leave those lines blank.

Copy the example file and open it in Notepad:

```powershell
copy .env.example .env
notepad .env
```

| `.env` name | What it is | Where to get it | What breaks without it |
| --- | --- | --- | --- |
| `POSTGRES_PASSWORD` | The database owner's password. Docker creates the database with it. | You invent it. Use something long; you will rarely type it. | The database container refuses to start, and `shannon migrate` cannot create tables. |
| `POSTGRES_APP_PASSWORD` | The password Shannon herself uses. Deliberately a different, weaker-privileged account: it cannot bypass the rules that keep one business's data away from another's. | You invent it. | `shannon run` cannot connect: "password authentication failed for user agent_org_app". |
| `DATABASE_URL` | Where Shannon finds the database, as her own account. | Already filled in; it reuses `POSTGRES_APP_PASSWORD`. Change it only if the database is not on this machine. | Every command that touches the database stops and says `DATABASE_URL is not set`. |
| `DATABASE_MIGRATOR_URL` | The same database, as the owner account. Used only to create tables and to register a business. | Already filled in; it reuses `POSTGRES_PASSWORD`. | `shannon migrate` and `shannon sync-config` fail with "permission denied". |

Credentials for later phases, listed so you know what is coming and what
each unlocks — **do not fill these in now**: `ITHRIVE_VEEQO_API_KEY`
(live stock instead of a saved export), `ITHRIVE_GMAIL_*` (reading NAR
confirmations directly), `ITHRIVE_SMTP_*` and `TWILIO_*` (sending you the
report and approval requests), `ANTHROPIC_API_KEY` (the wording of
reports), `ITHRIVE_NAR_*` and `ITHRIVE_DYNAREX_*` (staging supplier
carts), `ITHRIVE_SHOPIFY_*` and `ITHRIVE_QBO_*` (catalogue and
accounting). `.env.example` says where each is obtained. Never paste a
password into a source file; `.env` is the only place, and it is never
committed.

---

## 3. Start the database

With Docker Desktop running:

```powershell
docker compose up -d
```

Check it is healthy:

```powershell
docker compose ps
```

The `postgres` line should say `healthy`. Then create the tables:

```powershell
uv run shannon migrate
```

Expected, the first time:

```
Database updated: 0001_schema.sql, 0002_rls.sql, 0003_grants.sql, 0004_entity_scope_function.sql
```

Run it again and it says `Database is already up to date.` That is fine;
it is safe to run any number of times.

Copy the configuration into the database:

```powershell
uv run shannon sync-config
```

```
Copied the configuration into the database: bom_lines 135, channels 5, components 40, kits 12, suppliers 9
```

---

## 4. Check the configuration

```powershell
uv run shannon validate-config
```

This reads the parts lists, suppliers and policies and tells you, in
English, anything that would make a week's numbers wrong.

**A pass looks like this** — no `ERROR` lines at all, and a last line
saying so. This is what today's configuration produces:

```
Configuration for iThrive Medical LLC (ithrive)
BOM version: 2026-08-24
Configuration fingerprint: b6c77ba0b84081e0

42 components, 12 kits, 9 suppliers, 5 sales channels.

No problems. 16 warning(s) — worth reading, but nothing that stops a run.
```

Amazon identity is no longer among those warnings. `config/ithrive/listings.yaml`
holds Amazon's own SKUs for every kit and for the three C-A-T colourways,
and it is the only place they live — the `fba: TODO` placeholders that used
to sit in `boms.yaml` were removed rather than filled in, because two files
claiming the same fact eventually disagree. That closed parking-lot item
PL-8. The aliases still in `boms.yaml` are the SKUs **Veeqo** counts stock
under, which are yours and are a different thing.

One genuine gap remains and is still warned about: the Compact IFAK has no
Shopify SKU. `listings.yaml` speaks only for Amazon, so it cannot answer
that one, and until it is filled in the Compact IFAK's Shopify sales are
not counted. Each warning names the file and the line.

An `ERROR` looks like this — you should not see one today:

```
ERROR   config/ithrive/boms.yaml:294
        Kit '20-314' uses part 'CARD-TODO' from 'own_printed', but there is no
        component with that supplier and part number in this file.
        What to do: Add the component to the 'components:' block, or correct the
        part number on this line.
```

Warnings are not failures. A warning is something worth reading. An error
that concerns one line of the parts list does not stop the whole run: that
line is excluded from the arithmetic and listed under **BLOCKED** in the
report, so the rest of the week's numbers still arrive. An error that
makes the configuration unusable as a whole stops the run before anything
is read.

---

## 5. Run Shannon by hand

There is a schedule in the configuration (Mondays, 06:00), and you can
see it with `uv run shannon schedule`. In Phase 1 the manual trigger is
the one that matters:

```powershell
uv run shannon run
```

Expected:

```
Report written to reports\replenishment-2026-08-24-ithrive.txt
Nothing was ordered and nothing was sent. Read the report, then place any orders yourself.
```

The report lands in the `reports` folder inside the repository, named for
the date and the business. Open it in Notepad. It is plain text, meant to
be read start to finish.

By default the run reads the saved exports in
`tests/fixtures/golden/data`. To run against your own exports, point it
at them:

```powershell
uv run shannon run --fixtures C:\Users\Zach\Desktop\exports --output C:\Users\Zach\Desktop
```

That folder needs four files: `inventory.json`, `velocity.json`,
`fba_inbound.json` and `messages.json`. The files in
`tests/fixtures/golden/data` show the shape of each.

---

## 6. What to check in that first report

Read these seven things, in this order. If they are right, the report is
right.

1. **The header says READ ONLY**, and that nothing was ordered, staged,
   emailed or texted. If it does not, stop and say so.
2. **The BOM version** near the top matches the one `validate-config`
   printed. If they differ, the parts list changed between the two
   commands.
3. **Where the numbers came from** — the count of SKUs read from Veeqo
   looks like the size of the real export, and outstanding NAR orders are
   listed by order number. If an order you know shipped is listed as
   outstanding, the shipping email is missing from the export, not from
   reality.
4. **Each ordering line has five numbers**, e.g.
   `428 → 600 → 600 → 600 → 600`. They read: what is genuinely needed,
   then after the supplier's minimum order, then rounded up to the
   nearest 5, then converted into the units the supplier sells (a
   two-pack counts once), then how many individual items arrive. The
   first number should be roughly `demand − on hand − on order`; if it is
   not, something in the parts list is wrong.
5. **Build recommendations** name the part that runs out first for each
   colourway. That named part is what to chase.
6. **DEMAND SUPPRESSED.** Everything whose Amazon listings are all
   inactive is listed here rather than in the ordering list, because its
   recent sales measure the listing and not the demand. Shannon gives the
   figure from before the listing came down where the history reaches
   back, and says plainly when it does not — she never puts zero there and
   never forecasts these lines. Each one is also added to the parking lot,
   because only you can decide whether to restock and relist. Kits with no
   Amazon listing at all (20-314, 20-315, 25-002) are **not** here; their
   Amazon zero is simply true.
7. **The gap list and the parking lot.** The gap list is everything
   Shannon cannot order even in later phases — order it yourself. The
   parking lot is the open questions, carried week to week until you
   clear them.

Two things you may not expect to see. Amazon sales are matched on
**Amazon's own SKUs** (`Q3-MWFF-Y7P4` and the like), listed in
`config/ithrive/listings.yaml`, never on your part numbers and never on
the ASIN — three C-A-T colourways share ASINs that NAR owns, so the ASIN
cannot say which colour sold. And the two Orca pouches print as a product
name with "our reference" beside the code: Orca publishes no part numbers,
so that code means nothing to them and must never go on an order.

Anything Shannon could not calculate appears under **BLOCKED**, with the
reason. She never guesses.

---

## 7. When it fails — the three likeliest causes

**1. "DATABASE_URL is not set", or the run hangs and then says it cannot
connect.**
Docker Desktop is not running, or the database container is not up. Start
Docker Desktop, wait for the whale icon to settle, then:

```powershell
docker compose up -d
docker compose ps
```

If the `postgres` line is not `healthy`, look at its log:

```powershell
docker compose logs postgres
```

A complaint about `POSTGRES_PASSWORD` means `.env` is missing or that
line is blank.

**2. "password authentication failed for user agent_org_app".**
You changed `POSTGRES_APP_PASSWORD` in `.env` after the first `migrate`.
The database still holds the old one. Re-run:

```powershell
uv run shannon migrate
```

That sets the account's password to whatever `.env` now says.

**3. `validate-config` reports errors, or the report has a long BLOCKED
section.**
That is the intended behaviour, not a crash: a part Shannon cannot trust
is not counted. Fix the file and line named in the message and run
`validate-config` again. The known outstanding item today is the Compact
IFAK's missing Shopify SKU. That is a warning, not an error: the run still
happens, but its Shopify sales are not counted. Amazon SKUs are no longer
outstanding — they are in `config/ithrive/listings.yaml` (PL-8, closed).

A fourth, less likely: **"Veeqo export not found"** or a message about an
unreadable cell. The export folder is missing a file, or a cell that
should hold a number holds something else. Shannon stops rather than
guess; re-export from Veeqo and try again.

---

## 8. What Shannon cannot do in Phase 1

Deliberately, and enforced by tests, not by good intentions:

- no purchases, no checkout, no cart of any kind;
- no email, no SMS;
- no browsing of narescue.com or dynarex.com;
- no writes to Veeqo, Shopify, Amazon or anything else;
- no live account access at all — she reads saved exports.

Everything she produces is a recommendation in a text file and a copy of
that file in the database. Every order is still placed by a person.
