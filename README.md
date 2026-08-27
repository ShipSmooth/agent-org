# agent-org

The Org: a self-hosted agent system that runs business operations across
Zach's LLCs with tiered autonomy. Humans approve consequential actions;
everything else runs unattended. v1 ships one agent: **Shannon**, who
handles inventory replenishment for iThrive Medical — she stages a NAR
cart and writes a report. She never purchases.

**This repository contains Phase 0 (the architecture specification) and
Phase 1: the core runtime and Shannon's read-only replenishment run.** In
Phase 1 Shannon reads saved Veeqo and Gmail exports, does the reorder
arithmetic and writes a report to a file and to Postgres. She cannot buy,
send, browse or change anything anywhere: the cart, email, SMS and
approval paths are deliberately not built. Every action still passes
through the ActionBroker, which refuses anything above Tier 0 in this
phase.

Running it for the first time, written for a non-engineer:
[FIRST-RUN.md](FIRST-RUN.md).

## Read this first

- [docs/plain-english-overview.md](docs/plain-english-overview.md) — the
  whole system in plain English (start here)
- [docs/replenishment.md](docs/replenishment.md) — the reorder calculation,
  fully specified, with a worked numeric example
- [docs/live-data.md](docs/live-data.md) — what Veeqo and Gmail really
  return, what happens when they fail, the Monday email, and what a crash
  leaves behind
- [docs/policy.md](docs/policy.md) — the four autonomy tiers, default-deny
- [docs/architecture.md](docs/architecture.md) — processes, task state
  machine, failure model
- [docs/supplier-model.md](docs/supplier-model.md) — suppliers and their
  capabilities
- [docs/multi-entity.md](docs/multi-entity.md) — tenancy, RLS isolation,
  adding an LLC with zero code changes
- [docs/action-broker.md](docs/action-broker.md) — the single chokepoint
  for side effects
- [docs/agents.md](docs/agents.md) — Shannon, the one v1 agent, and her
  hard limits
- [docs/conventions.md](docs/conventions.md) — naming: the agent is
  Shannon (`shannon` in code); agents are never referred to only by
  function
- [docs/data-model.md](docs/data-model.md) — Postgres DDL and RLS policies
- [docs/adr/](docs/adr/) — decision records

## Development

Target host: Windows + Docker Desktop + WSL2. Development uses
[uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
make install      # create venv, install pinned deps
make lint         # ruff
make typecheck    # mypy --strict
make importcheck  # import-linter boundary contract
make test         # pytest (needs a Postgres for the RLS tests)
make check        # all of the above
docker compose up -d # postgres (see docker-compose.yml)
```

The `shannon` command:

```bash
uv run shannon validate-config   # check the parts lists, in plain English
uv run shannon migrate           # create or update the database tables
uv run shannon sync-config       # copy the configuration into the database
uv run shannon run               # this week's run → a report file
uv run shannon run --again       # work a finished week out afresh, as often as asked
uv run shannon resend            # email the report that already exists, unchanged
uv run shannon schedule          # what is scheduled, and whether it is due
```

Credentials come from environment variables only — copy `.env.example` to
`.env` and fill it in; the comments say where each value is obtained.
