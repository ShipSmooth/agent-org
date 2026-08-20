# Multi-entity tenancy

Multi-entity is foundational, not a feature: every table carries
`entity_id`, and isolation is enforced by the **database**, not by
remembering to add a WHERE clause.

## Entity registry

Entities are defined in config, one file per LLC under `config/entities/`:

```yaml
# config/entities/ithrive.yaml
entity_id: ithrive            # stable slug; the RLS key
legal_name: iThrive Medical LLC
status: active                # active | dormant
timezone: America/Chicago
agents:                       # which agents run for this entity
  - kind: replenishment
    schedule: "cron: 0 6 * * MON"
channels: [amazon_fba, amazon_fbm, shopify, walmart_sf, walmart_wfs]
suppliers_config: config/ithrive/suppliers.yaml
boms_config: config/ithrive/boms.yaml
replenishment_config: config/ithrive/replenishment.yaml
policy_config: config/ithrive/policy.yaml     # inherits global defaults
notifications:
  email: zach@shipsmooth.com
  sms: env:ZACH_SMS_NUMBER
credentials_prefix: ITHRIVE_                  # see credential isolation
books: quickbooks_online
```

A definition carries: identity, status, timezone, the agents it runs and
their schedules, its channels, paths to its per-entity configs (suppliers,
BOMs, replenishment parameters, policy overrides), notification targets,
its credential prefix, and its accounting system. ShipSmooth exists as
`status: dormant` with an empty `agents:` list — defined, running nothing.
Lima Zulu is registered with no agents until later phases (note for then:
Voly has no API, so its data path will be export-driven).

## Data isolation: entity_id + Postgres row-level security

Every business table has `entity_id TEXT NOT NULL REFERENCES entities(id)`.
RLS is enabled and **forced** on every such table (full DDL in
docs/data-model.md):

```sql
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;   -- applies even to the table owner
CREATE POLICY tasks_entity_isolation ON tasks
  USING (entity_id = current_setting('app.entity_id'))       -- note: NO second default arg
  WITH CHECK (entity_id = current_setting('app.entity_id'));
```

The application opens every unit of work with
`SET LOCAL app.entity_id = '<entity>'` inside a transaction.

**How an unscoped query fails loudly:** `current_setting('app.entity_id')`
is called *without* the `missing_ok` flag, so if the session variable was
never set, Postgres raises
`ERROR: unrecognized configuration parameter "app.entity_id"` — the query
**errors**; it does not silently return zero rows and it can never return
another entity's rows. This is deliberate: the two-argument form
`current_setting(..., true)` returns NULL and would make an unscoped query
silently empty — a bug that hides. We want the crash.

The application role has no `BYPASSRLS`, is not the table owner
(and tables are `FORCE`d anyway), and migrations run under a separate role.

## Credential isolation

All secrets come from environment variables (a literal in source fails CI).
Every per-entity credential is namespaced by the entity's
`credentials_prefix`: `ITHRIVE_VEEQO_API_KEY`, `ITHRIVE_NAR_USERNAME`,
`LIMAZULU_QBO_CLIENT_ID`, … The config loader resolves `env:` references
through the entity's prefix and **refuses to start** an entity whose
declared credentials are missing — and refuses to hand entity A's loader an
entity-B-prefixed variable. Shared infrastructure credentials (Postgres,
SMTP, SMS) are unprefixed. No credential is ever stored in the database.

## Memory partitioning

Anything an agent remembers — run transcripts (`agent_runs`), internal
notes, and any future pgvector embeddings — lives in RLS-scoped tables
carrying `entity_id`. An iThrive agent physically cannot retrieve Lima Zulu
memory: the same `app.entity_id` mechanism gates it. There is no shared
"global memory" store; anything genuinely global (e.g. code, prompts) is in
the repo, not the database.

## Adding a fourth LLC — zero code changes

1. Write `config/entities/newco.yaml` (copy iThrive's, edit values).
2. Insert the registry row (idempotent sync on startup does this from
   config: `INSERT ... ON CONFLICT DO NOTHING` into `entities`).
3. Add the entity's credentials to the host `.env` under its prefix
   (`NEWCO_VEEQO_API_KEY=`, …) per `.env.example`.
4. Add its per-entity config files (suppliers, BOMs, replenishment
   parameters, policy overrides) under `config/newco/`.
5. `docker compose restart worker scheduler`.

No migration, no new table, no Python change. RLS policies reference the
session variable, not an entity list, so they cover the new entity
automatically.

## Acceptance test (written, runnable)

`tests/test_multi_entity_acceptance.py::test_add_entity_with_zero_code_changes`

1. **Given** a running system with `ithrive` seeded and one completed
   replenishment task.
2. **When** the test writes `config/entities/testco.yaml` to a temp config
   dir, sets `TESTCO_*` env vars to fixtures, and calls the same startup
   sync the worker calls — importing no new modules and touching no source
   file (the test asserts `git status --porcelain` on `src/` is empty).
3. **Then**:
   a. `entities` contains `testco`; the scheduler enqueues its task.
   b. With `app.entity_id = 'testco'`, queries see only testco rows;
      with `'ithrive'`, iThrive's task history is unchanged and contains
      no testco rows.
   c. A connection that never sets `app.entity_id` gets a raised database
      error (asserted with `pytest.raises`) on `SELECT * FROM tasks` —
      not an empty result.
   d. A testco replenishment dry-run completes using testco fixtures and
      writes audit rows carrying `entity_id = 'testco'` only.

If any step requires editing a `.py` file, the test — and the promise —
fails.
