# ADR-0007: NAR integration via browser automation, staging only

**Status:** Accepted · **Date:** 2026-08-20

## Context
narescue.com has no API (confirmed with the vendor). Sessions expire
frequently and require clicking a login button. Freight is LTL, auto-quoted
only at checkout. Typical orders run $15k–$65k.

## Decision
A headless Chromium (Playwright) executor, owned by the broker, logs in
with credentials from environment variables (Chrome's saved passwords are
unreachable from a container), stages the cart, and captures the freight
quote into the proposal result. v1 grants NAR `stage_cart` but **not**
`purchase`: Shannon never checks out.

## Consequences
- Inherently brittle: site redesigns break selectors. Mitigated by staging
  being harmless (a cart costs nothing), step-level audit logging, and
  loud failure with screenshots on selector misses.
- Freight is discovered, never predicted; Zach decides with the quote in
  hand.
- Revisit if NAR ever ships an API or EDI channel.
