# agent-org

The Org: a self-hosted agent system that runs business operations across
Zach's LLCs with tiered autonomy. Humans approve consequential actions;
everything else runs unattended. v1 ships one agent — inventory
replenishment for iThrive Medical — which stages a NAR cart and writes a
report. It never purchases.

**This repository currently contains Phase 0 only: the architecture
specification, a package skeleton, and tooling. There is no application
code yet.**

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
- [docs/agents.md](docs/agents.md) — the one v1 agent and its hard limits
- [docs/data-model.md](docs/data-model.md) — Postgres DDL and RLS policies
- [docs/adr/](docs/adr/) — decision records

## Development

Target host: Windows + Docker Desktop + WSL2. Development uses
[uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
make install      # create venv, install pinned deps
make lint         # ruff
make typecheck    # mypy --strict
make test         # pytest (no tests yet in Phase 0)
make check        # all of the above
docker compose up # postgres skeleton (see docker-compose.yml)
```

Credentials come from environment variables only — copy `.env.example` to
`.env` and fill it in; the comments say where each value is obtained.
