# ADR-0002: Postgres only — datastore, queue, and (if needed) vectors

**Status:** Accepted · **Date:** 2026-08-20

## Context
One home-office host, a handful of tasks per week, and a hard audit
requirement. Candidate additions: Redis (queue/cache), a vector DB service.

## Decision
Postgres is the only datastore. The task queue is a table claimed with
`SELECT ... FOR UPDATE SKIP LOCKED`. If embeddings are ever needed,
pgvector — never a separate service.

## Consequences
- One backup, one failure domain, one thing to administer over Tailscale.
- Queue state is inspectable with SQL and covered by the same audit and
  RLS story as everything else.
- Not web-scale. Irrelevant at this volume; revisit only with evidence.
