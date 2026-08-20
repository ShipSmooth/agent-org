# ADR-0001: Plain Python agents, no agent framework

**Status:** Accepted · **Date:** 2026-08-20

## Context
Frameworks (LangGraph, CrewAI, AutoGen) offer orchestration, retries, and
checkpointing out of the box. The Org's hard requirements are auditability
(single broker path, write-ahead audit, default-deny tiers) and longevity
on a codebase Zach cannot read himself.

## Decision
Agents are plain Python classes over one thin `LLMClient` abstraction.
Orchestration is a Postgres `tasks` table plus an explicit state enum.

## Consequences
- We reimplement a small amount of orchestration (~hundreds of lines).
- The "no second path to the outside world" guarantee is provable by
  reading our own code and enforced in CI, without auditing a framework on
  every version bump.
- No framework migration risk across framework churn.
- Reversible: if the Org grows genuinely graph-shaped multi-agent
  workflows, revisit; the broker/policy/audit layers are framework-agnostic
  and would survive the change.
