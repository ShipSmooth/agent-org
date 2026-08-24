# FIRST-RUN.md — getting Shannon running on the Dell

This is the setup guide for the first live run of **Shannon**, the iThrive
Medical replenishment agent. It assumes no programming knowledge. Follow it
top to bottom.

A word on what Shannon can do at this point, because it matters: she can
**read data, do the arithmetic, and write a report**. That is all. She cannot
send email or text messages, cannot log into narescue.com or dynarex.com,
cannot put anything in a cart, and cannot change a single number in Veeqo,
Shopify or Amazon. That is deliberate — this phase builds the brain, not the
hands.

Words used below:

- **Docker** — software that runs the database in a self-contained box, so
  nothing has to be installed into Windows itself.
- **WSL2** — the piece of Windows that lets Docker run. Docker Desktop turns
  it on for you.
- **Terminal / PowerShell** — the window where you type commands.
- **Repository (repo)** — the folder holding this code.
- **`.env`** — a plain text file holding passwords and keys. It never goes
  into the code, and it is never committed.
- **Migration** — the step that creates the database tables.

---

## 1. What to install on the Dell, in order

1. **Docker Desktop for Windows**
   Download: https://www.docker.com/products/docker-desktop/
   During install, leave "Use WSL 2 instead of Hyper-V" ticked. Restart the
   machine when it asks. Open Docker Desktop once and wait until the whale
   icon in the system tray stops animating — it must say "Engine running".

2. **Git for Windows**
   Download: https://git-scm.com/download/win — accept every default.

3. **Python 3.12**
   Download: https://www.python.org/downloads/release/python-3129/ (the
   "Windows installer (64-bit)"). On the first screen, tick **"Add python.exe
   to PATH"** before clicking Install.

4. **uv** — the tool that installs this project's Python packages.
   Open **PowerShell** and run:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   Close PowerShell and open a fresh one afterwards, so it picks up `uv`.

5. **Get the code and install it.** In PowerShell:

   ```powershell
   cd $HOME
   git clone https://github.com/ShipSmooth/agent-org.git
   cd agent-org
   uv sync
   ```

   `uv sync` takes a couple of minutes the first time. It is finished when
   you get the prompt back with no red text.

---

## 2. The `.env` file — every credential, where to get it, what breaks without it

Copy the template and then edit it:

```powershell
copy .env.example .env
notepad .env
```

Fill in a value after each `=`, no quotes, no spaces around the `=`. Save and
close Notepad when done.

**Do not send any of these values to anyone, including Devin.** They belong on
the Dell only.

### Needed for the very first run (this phase)

| Name in `.env` | Where to get it | What breaks without it |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | You invent it. Any 20+ random characters, letters and digits. Write it in your password manager. | Nothing starts at all — the database container refuses to boot. |
| `DATABASE_URL` | Leave the line exactly as `.env.example` has it; it reuses `POSTGRES_PASSWORD`. On the Dell, running commands outside Docker, change `@postgres:` to `@127.0.0.1:`. | Shannon cannot reach the database and says so. |

That is genuinely all you need to produce the first report against test data.

### Needed when Shannon starts reading live data (next phase)

| Name in `.env` | Where to get it | What breaks without it |
| --- | --- | --- |
| `ITHRIVE_VEEQO_API_KEY` | Veeqo web app → Settings → Users → your user → API Keys → create. | No stock, no 90-day velocity, no inbound. The run stops; it never guesses at inventory. |
| `ITHRIVE_GMAIL_CLIENT_ID` | https://console.cloud.google.com → APIs & Services → Credentials → Create OAuth client (Desktop app), with the Gmail API enabled, read-only scope. | Shannon cannot see NAR order confirmations, so she cannot tell what is already on order — and the whole point is not ordering twice. The run stops. |
| `ITHRIVE_GMAIL_CLIENT_SECRET` | Same screen as the client ID. | Same as above. |
| `ITHRIVE_GMAIL_REFRESH_TOKEN` | Produced once by signing in to the Google consent screen with **zach@ithrivemedical.com**. | Same as above. |
| `ITHRIVE_SHOPIFY_STORE_DOMAIN` | Shopify admin → Settings → Domains, e.g. `ithrive-medical.myshopify.com`. | Product/BOM lookups fall back to config only. Shopify stock numbers are placeholders and are never used for stock or velocity. |
| `ITHRIVE_SHOPIFY_ADMIN_TOKEN` | Shopify admin → Settings → Apps and sales channels → Develop apps → create an app → Admin API access token. | Same as above. |

### Not needed yet — leave blank

`APPROVAL_TOKEN_SECRET`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, all
`TWILIO_*`, all `ITHRIVE_SMTP_*`, `ITHRIVE_ZACH_SMS_NUMBER`, all
`ITHRIVE_QBO_*`, `ITHRIVE_NAR_*`, `ITHRIVE_DYNAREX_*`.

Shannon has no code that sends mail, sends SMS, logs into a supplier site or
touches a cart, so these are inert. They are listed in `.env.example` so the
naming scheme is visible when those phases are built.

---

## 3. Start the database and create the tables

With Docker Desktop running:

```powershell
cd $HOME\agent-org
docker compose up -d
uv run shannon migrate
```

A good result looks like:

```
Applied: 0001_schema.sql, 0002_rls.sql.
```

Run it a second time and it should say `Applied: nothing — up to date.` That
is normal and safe: migrations never run twice.

---

## 4. Check the configuration

```powershell
uv run shannon validate-config
```

This reads `config/ithrive/*.yaml` and checks every rule: that every BOM line
points at a component that exists, that ASINs look like ASINs, that channels
are real channels, that nothing routed for purchase has an unknown supplier,
and that reorder thresholds make sense.

A pass looks like this:

```
bom_version: 2026-08-20
WARNING  config/ithrive/boms.yaml:28: Supplier 'dynarex' has no lead time yet ...

validate-config: OK — 0 errors, 16 warning(s). Shannon can run against this configuration.
```

**Warnings are fine.** They are the known gaps — missing lead times, thresholds
you have not set yet — and they appear on the report's gap list rather than
stopping anything.

**Errors are not fine, and today there are some on purpose.** As committed, the
configuration has two known holes, and validate-config names both, with file
and line:

```
ERROR  config/ithrive/boms.yaml:294: Kit 20-314 uses own_printed/CARD-TODO, but no
       component with that supplier and part number exists.
ERROR  config/ithrive/boms.yaml:285: Kit 20-314 has no real SKU for its 'fba'
       listing — it is still TODO.
```

Those are real, outstanding decisions (parking-lot items PL-4 and PL-8): the
instruction cards have no supplier record, and several kits have no Amazon
SKU. Shannon refuses to run on the live configuration until they are filled
in, because a kit with a placeholder SKU cannot have its sales matched
correctly. Fixing them means editing the named lines in
`config/ithrive/boms.yaml` — no code change.

The command exits with a failure code when there are errors, which is what
makes it safe to run before every scheduled run.

---

## 5. Do a run and read the report

The first run uses the built-in test data (`tests/fixtures/golden/`), so it
proves the machinery end to end without needing a single live credential:

```powershell
uv run shannon run --config tests\fixtures\golden\config --fixtures tests\fixtures\golden\data --out reports
```

You should see:

```
Run complete. Report: reports\shannon-ithrive-2026-W35-manual.md
```

Open that file in Notepad. The same text is also stored in the database, so
the file can be deleted without losing anything.

`--config` says where the product and BOM configuration lives, `--fixtures`
says where the data files live, `--out` says where to put the report. When
live reading is switched on, only `--fixtures` changes.

### What to check in that first report

1. **Header** — the BOM version and the run slot are named, and the line
   confirming nothing was sent, staged or bought.
2. **Parameters used** — `cover_target_weeks: 7 (inclusive of lead time)`,
   `velocity_window_days: 90`. If those are wrong, every number below is wrong.
3. **Forecast purchase lines** — each line shows every rounding step in order:
   raw net requirement → MOQ-rounded → nearest-5 → purchase units → actual
   units. Check one by hand. The test data is the worked example from the
   documents, so `30-0001` should read 588 gross, 428 net, 600 ordered.
4. **Purchase units versus order units** — the number you would actually buy
   is the purchase-unit column. A component sold in 2-packs shows 245 needed
   and 65 two-packs; ordering 245 of them would be four times too much.
5. **Build recommendations** — each blocked build names the one component
   holding it up.
6. **Gap list and parking lot** — everything Shannon could not decide. This is
   your to-do list, and it should shrink week to week.

---

## 6. When it goes wrong — the three likeliest failures

**"Cannot migrate: no database URL configured" or "connection refused"**
Docker is not running, or `.env` is missing. Check the whale icon says
"Engine running", then `docker compose ps` — the postgres line should say
`healthy`. If `.env` has `@postgres:5432` and you are running commands from
PowerShell rather than inside Docker, change it to `@127.0.0.1:5432`.

**"validate-config: FAILED — N error(s)"**
Not a crash: the configuration has a problem, and each line names the file and
line number to edit. Fix those lines in `config/ithrive/boms.yaml`, save, and
run `uv run shannon validate-config` again. Nothing else runs until this
passes.

**"Run stopped: ..."**
Shannon deliberately stops rather than guess. The commonest reasons:

- *An order's signals are ambiguous* — two different confirmation emails for
  the same NAR order number, or a shipping notification she cannot match to a
  confirmation. Look at the order number in the message in Gmail and see
  whether it shipped. Guessing here means ordering twice.
- *A run for that slot already happened* — she never repeats a week. Pass a
  different `--slot`, e.g. `--slot 2026-W36-manual`.
- *A read failed* — Veeqo or Gmail did not answer, or a key expired. Re-run;
  if it repeats, the credential in `.env` is the first thing to check.

A stopped run is safe. Nothing is half-done: the task is recorded as failed
with the reason, and no report claiming to be complete is written.

---

## 7. The weekly schedule

The schedule is Monday 06:00 New York time. Nothing runs on a timer until a
scheduled trigger is wired on the Dell; for now the manual command above is
the way to run her. To ask whether this week's run is due:

```powershell
uv run shannon schedule-tick
```

It either enqueues this week's run or tells you when the next one is. Running
it repeatedly is harmless — one run per week, no duplicates.
