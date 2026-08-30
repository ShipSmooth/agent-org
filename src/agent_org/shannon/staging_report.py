"""The confirmation report: what went into a supplier's cart, and what did not.

Deliberately the same shape as the weekly report — a summary anyone can act
on at the top, the detail below — because it arrives in the same inbox and
answers the same kind of question. What it adds is the sentence Zach asked
for and this system exists to be able to say honestly: nothing was
submitted, nothing was paid for, no order was placed.

A dry run says "would be added" everywhere a live run says "added". The
two reports are never mistaken for each other, and the mode is on the
subject line as well as in the body.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_org.shannon.staging import StagingPlan

RULE = "=" * 78
THIN = "-" * 78

MODE_DRY_RUN = "DRY_RUN"

NOTHING_WAS_SUBMITTED = (
    "NOTHING WAS SUBMITTED. No checkout was started, no payment details were "
    "entered, and no order was placed. The cart is waiting for you to review "
    "it and order it yourself."
)


@dataclass(frozen=True)
class StagingContext:
    entity_name: str
    supplier_name: str
    week: str
    generated_at: datetime
    report_week_of: str | None = None


def subject_line(supplier_name: str, week: str, staged: int, failed: int, dry_run: bool) -> str:
    """What Zach reads with the laptop shut."""
    what = "would stage" if dry_run else "staged"
    if staged == 0:
        headline = f"{what} nothing"
    elif staged == 1:
        headline = f"{what} 1 line"
    else:
        headline = f"{what} {staged} lines"
    if failed:
        headline += f", {failed} failed"
    rehearsal = " (dry run)" if dry_run else ""
    return f"Shannon — {supplier_name} cart — week of {week} — {headline}{rehearsal}"


def _wrapped(text: str, indent: str = "  ") -> list[str]:
    return textwrap.wrap(text, width=76, initial_indent=indent, subsequent_indent=indent)


def _money(value: Any, currency: str) -> str:
    return "not given by the site" if value in (None, "") else f"{currency} {value}"


def _count(lines: int) -> str:
    return "1 line" if lines == 1 else f"{lines} lines"


def render(plan: StagingPlan, result: dict[str, Any], context: StagingContext) -> str:
    """The confirmation report, in full."""
    dry_run = str(result.get("mode")) == MODE_DRY_RUN
    lines_out: list[dict[str, Any]] = list(result.get("lines", []))
    added = [line for line in lines_out if line["status"] in ("ADDED", "PLANNED")]
    failed = [line for line in lines_out if line["status"] == "FAILED"]
    skipped = [line for line in lines_out if line["status"] == "SKIPPED"]
    before = dict(result.get("cart_before", {}))
    after = dict(result.get("cart_after", {}))
    verb = "Would add" if dry_run else "Added"

    out: list[str] = [
        RULE,
        f"  {context.supplier_name.upper()} CART — {'DRY RUN' if dry_run else 'STAGED'}",
        f"  {context.entity_name} — week of {context.week}",
        f"  Prepared by Shannon, {context.generated_at:%A %d %B %Y %H:%M} UTC",
        RULE,
        "",
        "AT A GLANCE",
        THIN,
        f"  {verb} {_count(len(added))} to the {context.supplier_name} cart.",
    ]
    if failed:
        out.append(f"  {_count(len(failed))} could NOT be added — listed below.")
    if skipped:
        out.append(f"  {_count(len(skipped))} were already staged this week and left alone.")
    if plan.skipped:
        out.append(f"  {_count(len(plan.skipped))} cannot be ordered by SKU — order those by hand.")
    unverified = [line for line in added if line.get("verified") is False]
    if unverified:
        out += _wrapped(
            f"CHECK THE CART YOURSELF: {_count(len(unverified))} went in, but the cart "
            "afterwards does not hold what it should. Details below."
        )
    lost = list((result.get("kept") or {}).get("lost", []))
    if lost:
        out += _wrapped(
            f"CHECK THE CART YOURSELF: the cart held {_count(len(lost))} before this run "
            "that it no longer holds in full. Shannon cannot remove a line, so the "
            "site did that."
        )
    if dry_run:
        out += _wrapped(
            "This was a DRY RUN. The cart was read and nothing in it was changed. "
            "No line was added."
        )
    out += ["", *_wrapped(NOTHING_WAS_SUBMITTED), "", ""]

    out += [f"{verb.upper()} — from this week's WHAT TO ORDER", THIN]
    if not added:
        out.append("  Nothing. No line in this week's report is routed to this cart.")
    for line in added:
        out.append(f"  {line['sku']}  {line['name']}")
        out.append(f"      quantity: {line['quantity']}")
        if line.get("verified") is True:
            out.append("      checked against the cart afterwards: it is there")
        if line.get("staged_sku") and line["staged_sku"] != line["sku"]:
            # The cart's own answer, which for a configurable product is the
            # only trustworthy statement of what is in it.
            out.append(f"      the cart records this as {line['staged_sku']}")
        out.append(f"      {line['detail']}")
    out.append("")

    if failed:
        out += ["", "COULD NOT BE ADDED — order these by hand", THIN]
        for line in failed:
            out.append(f"  {line['sku']}  {line['name']}  ({line['quantity']})")
            out.append(f"      {line['detail']}")
        out.append("")

    if skipped:
        out += ["", "ALREADY STAGED THIS WEEK — not added again", THIN]
        for line in skipped:
            out.append(f"  {line['sku']}  {line['name']}  — {line['detail']}")
        out.append("")

    if plan.skipped:
        out += ["", "NOT STAGEABLE — no supplier SKU to add", THIN]
        for skip in plan.skipped:
            out.append(f"  {skip.key}  {skip.name}")
            out.append(f"      {skip.reason}")
        out.append("")

    out += ["", "THE CART BEFORE THIS RUN", THIN]
    out += _cart_block(before, "  Nothing was in the cart.")
    out += [
        "",
        "  Anything already in the cart is yours and was left exactly as it was.",
        "",
        "",
        "THE CART NOW" if not dry_run else "THE CART NOW (unchanged — this was a dry run)",
        THIN,
    ]
    out += _cart_block(after, "  The cart is empty.")

    out += [
        "",
        "",
        "WHAT HAPPENS NEXT",
        THIN,
        f"  Open {context.supplier_name} in your browser, check the cart against this",
        "  report, and place the order yourself. Shannon does not check out, and",
        "  will not, at any tier: she has no capability to buy and the code that",
        "  talks to the site refuses checkout, order and payment paths outright.",
        "",
        *_wrapped(NOTHING_WAS_SUBMITTED),
        "",
        RULE,
    ]
    return "\n".join(out)


def _cart_block(cart: dict[str, Any], empty: str) -> list[str]:
    lines = list(cart.get("lines", []))
    currency = str(cart.get("currency") or "USD")
    if not lines:
        block = [empty]
    else:
        block = [
            f"  {line['sku']}  {line['name']}  × {line['quantity']}"
            + (f"  @ {currency} {line['price']}" if line.get("price") else "")
            for line in lines
        ]
    block.append(f"  cart total: {_money(cart.get('grand_total'), currency)}")
    return block


__all__ = ["NOTHING_WAS_SUBMITTED", "StagingContext", "render", "subject_line"]
