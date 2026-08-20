# Conventions

## Naming: agents have names, not functions

The iThrive Medical replenishment agent is **Shannon**. This is her name,
permanently — it does not change with refactors, new agents, or new
sessions. Nothing in this repository refers to her by her function.

- In code and configuration the canonical identifier is `shannon`: module,
  package and directory names; config file names and top-level config keys;
  database values and enum members identifying the agent; log prefixes;
  environment-variable prefixes where she needs her own; git branch names
  for work on her; metric and alert names.
- In everything a human reads she is **Shannon**: the "from" name and
  signature on every email she sends; the sender identity on every SMS;
  dashboard headings and any UI naming her; report titles and bodies;
  commit messages, PR titles and descriptions; every document in this repo.
- Never "the replenishment agent", "the reorder bot", "the NAR agent",
  "the agent", or `agent_1`. If a sentence needs a common noun, write
  "Shannon, the replenishment agent" once, then "Shannon".
- Where a generic term is genuinely required — a base class, an abstract
  interface, a table holding many agents — use `Agent` for the general
  concept and reserve `shannon` for her.
- She uses she/her. Agents in this system get human names and human
  pronouns because the operator interacts with them conversationally and
  needs to know at a glance which one is speaking. Later agents are named
  the same way; no agent is ever referred to only by its function.

This applies retroactively: any file that refers to her by function is
renamed the next time it is touched.

## Placeholder identifiers

Never invent SKUs, part numbers, or ASINs that look real. Where a real
identifier is not yet known, use an obvious placeholder (`TBD-1`,
`TBD-ASIN-1`) so it cannot be mistaken for data.
