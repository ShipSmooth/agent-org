# agent-org

The Org: a self-hosted agent system that runs business operations across
Zach's LLCs with tiered autonomy. Humans approve consequential actions;
everything else runs unattended. v1 ships one agent: **Shannon**, who
handles inventory replenishment for iThrive Medical — she stages a NAR
cart and writes a report. She never purchases.

**This repository is at Phase 1: the brain, and only the brain.** Shannon
reads, calculates and writes a report — to a file and to the database. She
cannot send, buy, browse or change anything anywhere; no code exists for any
of it. Cart staging, approvals, email and SMS are later phases.

New to the project, or setting up the machine that runs her?
[FIRST-RUN.md](FIRST-RUN.md) is the non-engineer's guide from bare Windows to
first report.

## Read this first

- [docs/plain-english-overview.md](docs/plain-english-overview.md) — the
  whole system in plain English (start here)
- [docs/replenishment.md](docs/replenishment.md) — the reorder calculation,
  fully specified, with a worked numeric example
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
make test         # pytest — database tests skip without TEST_DATABASE_URL
make check        # all of the above
docker compose up # postgres (see docker-compose.yml)
```

Shannon's commands:

```bash
shannon migrate                     # create/upgrade the schema
shannon validate-config             # every docs/replenishment.md §13 check
shannon run --config tests/fixtures/golden/config \
           --fixtures tests/fixtures/golden/data --out reports
shannon schedule-tick               # enqueue this week's run if it is due
```

Credentials come from environment variables only — copy `.env.example` to
`.env` and fill it in; the comments say where each value is obtained.
