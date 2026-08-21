# ADR-0003: Multi-entity isolation via forced Postgres row-level security

**Status:** Accepted · **Date:** 2026-08-20

## Context
Multiple LLCs share one system. Application-level `WHERE entity_id = ?`
filtering fails silently the first time someone forgets it.

## Decision
Every business table carries `entity_id`. RLS is ENABLEd and FORCEd with
policies keyed on `current_setting('app.entity_id')` in its one-argument
form, set per transaction via `SET LOCAL`. Adding an LLC is a config file
(entity registry YAML + prefixed env credentials), never a code change.

## Consequences
- A query without an entity context **raises an error** — loud failure
  rather than silently returning empty or, worse, another entity's rows.
- Isolation is enforced by the database even against buggy application
  code; app role has no BYPASSRLS and owns no tables.
- Slight ceremony: every unit of work must open with the SET LOCAL; a
  helper owns this and tests assert the unscoped-query failure.
