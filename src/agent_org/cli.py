"""The `shannon` command.

Everything it prints is meant to be read by someone who is not an
engineer. No tracebacks: a failure is a sentence saying what went wrong,
in which file, and what to do about it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from agent_org.config.errors import ConfigError
from agent_org.config.loader import load_config
from agent_org.config.models import LoadedConfig
from agent_org.config.validate import validate
from agent_org.db.connection import (
    DatabaseNotConfigured,
    DatabaseSettings,
    connect,
    entity_session,
    set_app_password,
)
from agent_org.db.migrate import run_migrations
from agent_org.db.sync import sync_config
from agent_org.env import load_env_file
from agent_org.integrations.carts import CartRefusal, CartUnavailable
from agent_org.integrations.reads import ReadFailure
from agent_org.runtime.staging import (
    NothingToStage,
    deliver_staging_report,
    stage_supplier_cart,
)
from agent_org.runtime.worker import (
    NothingToResend,
    RunAlreadyDone,
    deliver_report,
    resend_report,
    run_replenishment,
)
from agent_org.scheduler.schedule import ScheduleError, is_due
from agent_org.shannon.config_diff import ConfigSnapshot, describe_changes
from agent_org.tenancy.registry import register_entity

EXIT_OK = 0
EXIT_PROBLEM = 1


def _load(args: argparse.Namespace) -> LoadedConfig:
    return load_config(Path(args.config_root), args.entity)[0]


def cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        config, findings = load_config(Path(args.config_root), args.entity)
    except ConfigError as exc:
        for finding in exc.findings:
            print(finding.render())
        print("\nThe configuration could not be read. Nothing was run.")
        return EXIT_PROBLEM

    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
    except ValueError:
        print(f"--as-of wants a date written as 2026-08-26, not '{args.as_of}'.")
        return EXIT_PROBLEM
    result = validate(config, findings, today=as_of)
    print(f"Configuration for {config.entity.legal_name} ({config.entity_id})")
    print(f"BOM version: {result.bom_version}")
    snapshot = ConfigSnapshot.of(config)
    print(f"Configuration fingerprint: {snapshot.digest}")
    print(describe_changes(snapshot, None) if args.verbose else "")
    print(
        f"{len(config.boms.components)} components, {len(config.boms.kits)} kits, "
        f"{len(config.boms.suppliers)} suppliers, "
        f"{len(config.entity.channels)} sales channels."
    )
    print("")

    for finding in result.errors:
        print(finding.render())
        print("")
    for finding in result.warnings:
        print(finding.render())
        print("")

    if result.errors:
        print(
            f"{len(result.errors)} problem(s) and {len(result.warnings)} warning(s). "
            "Fix the problems above; Shannon will not run a week's numbers on a "
            "parts list she cannot trust."
        )
        return EXIT_PROBLEM
    print(
        f"No problems. {len(result.warnings)} warning(s) — worth reading, but nothing "
        "that stops a run."
    )
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    try:
        settings = DatabaseSettings.from_env()
    except DatabaseNotConfigured as exc:
        print(str(exc))
        return EXIT_PROBLEM
    with connect(settings.migrator_dsn) as conn:
        result = run_migrations(conn)
        if settings.app_password:
            set_app_password(conn, settings.app_password)
        conn.commit()
    if not settings.app_password:
        print(
            "POSTGRES_APP_PASSWORD is blank, so Shannon's own database account "
            "was left as it was. Fill it in and run `shannon migrate` again, or "
            "`shannon run` will be refused with a password error."
        )
    if result.applied:
        print("Database updated: " + ", ".join(result.applied))
    else:
        print("Database is already up to date.")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    config = _load(args)
    try:
        settings = DatabaseSettings.from_env()
    except DatabaseNotConfigured as exc:
        print(str(exc))
        return EXIT_PROBLEM
    # Registering a business writes to `entities`, the registry the whole
    # isolation scheme keys off. The application role may only read it, so
    # that one row goes in as the owner.
    with connect(settings.migrator_dsn) as owner:
        register_entity(owner, config.entity)
        owner.commit()

    with connect(settings.app_dsn) as conn:
        with entity_session(conn, config.entity_id) as scoped:
            counts = sync_config(scoped, config)
        conn.commit()
    print(
        "Copied the configuration into the database: "
        + ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    try:
        settings = DatabaseSettings.from_env()
    except DatabaseNotConfigured as exc:
        print(str(exc))
        return EXIT_PROBLEM

    # --fixtures "" means the live Veeqo account and the live mailbox. Both
    # are read-only, and both take their credentials from the environment.
    fixtures = Path(args.fixtures) if args.fixtures else None
    try:
        with connect(settings.app_dsn) as conn:
            with entity_session(conn, config.entity_id) as scoped:
                summary = run_replenishment(
                    conn=scoped,
                    config=config,
                    fixtures=fixtures,
                    output_dir=Path(args.output),
                    now=datetime.now(tz=UTC),
                    again=bool(args.again),
                )
            # The report row and the file are committed here, before
            # anything is sent. The email that follows is a second
            # transaction, so a mail server having a bad minute cannot take
            # the week's report down with it.
            conn.commit()
            if summary.outcome is not None and not args.no_email:
                with entity_session(conn, config.entity_id) as scoped:
                    summary = deliver_report(conn=scoped, config=config, summary=summary)
                conn.commit()
    except ConfigError as exc:
        for finding in exc.findings:
            print(finding.render())
        print("\nThe run stopped before reading anything. Nothing was changed.")
        return EXIT_PROBLEM
    except ReadFailure as exc:
        print(str(exc))
        return EXIT_PROBLEM
    except RunAlreadyDone as exc:
        print(str(exc))
        return EXIT_PROBLEM

    if summary.error is not None:
        print(f"The run stopped: {summary.error}")
        return EXIT_PROBLEM
    print(f"Report written to {summary.report_path}")
    if summary.superseded_report_id is not None:
        # Plainly which run this replaced: two reports for one week is
        # confusing only when nothing says which is the live one.
        print(
            f"This replaces the report written at {summary.superseded_written_at}, "
            f"now kept as {summary.superseded_path} and marked in the database as "
            f"superseded (report {summary.superseded_report_id}). Both are kept."
        )
    if summary.email_error is not None:
        # Loud, and not a failed run. The report exists; it did not arrive.
        print("")
        print(f"The report was NOT emailed: {summary.email_error}")
        print(
            "The report itself is written and safe, and the failed attempt is "
            "recorded in the database. Fix the mail settings, then run "
            "`shannon resend` to send this same report — the numbers in it are "
            "not wrong, they just did not arrive. `shannon run --again` works the "
            "week out afresh instead, which is what you want if something changed."
        )
        return EXIT_PROBLEM
    if summary.emailed_to:
        print(f"Emailed to {', '.join(summary.emailed_to)} — subject: {summary.email_subject}")
    print(
        "Nothing was ordered, no cart was staged and no supplier was contacted. "
        "Read the report, then place any orders yourself."
    )
    return EXIT_OK


def cmd_resend(args: argparse.Namespace) -> int:
    """Put an already-written report in the post again.

    Deliberately separate from `run`: this one cannot read Veeqo, cannot
    calculate and cannot write a report. It sends bytes that were filed
    earlier, so what arrives is provably what was worked out at the time.
    """
    config = _load(args)
    try:
        settings = DatabaseSettings.from_env()
    except DatabaseNotConfigured as exc:
        print(str(exc))
        return EXIT_PROBLEM

    try:
        with connect(settings.app_dsn) as conn:
            with entity_session(conn, config.entity_id) as scoped:
                summary = resend_report(
                    conn=scoped,
                    config=config,
                    week=args.week,
                    now=datetime.now(tz=UTC),
                )
            # Committed whether it sent or not: a failed attempt is a row
            # worth keeping, and losing it would make the next failure look
            # like the first.
            conn.commit()
    except NothingToResend as exc:
        print(str(exc))
        return EXIT_PROBLEM

    if summary.previous is not None and summary.previous.error is not None:
        print(
            f"The last attempt on this report, at "
            f"{summary.previous.attempted_at:%d %b %Y %H:%M} UTC, failed: "
            f"{summary.previous.error}"
        )
    if summary.error is not None:
        print(f"The report was NOT emailed: {summary.error}")
        print(
            f"The report itself is untouched at {summary.report.file_path}, and "
            "this attempt is recorded in the database beside the others."
        )
        return EXIT_PROBLEM
    print(
        f"Emailed the report for {summary.week} to "
        f"{', '.join(summary.recipients)} — subject: {summary.subject}"
    )
    print(
        f"Nothing was worked out again: this is the report written at "
        f"{summary.report.written_at:%d %b %Y %H:%M} UTC, sent as it stands. "
        "Nothing was ordered and no supplier was contacted."
    )
    return EXIT_OK


def cmd_stage(args: argparse.Namespace) -> int:
    """Put this week's reported lines in a supplier's cart, or rehearse it.

    It calculates nothing. It reads the report Shannon already wrote for
    the week and acts on the lines that report routes to this supplier's
    cart, which is why a wrong number is fixed with `shannon run --again`
    and never here.
    """
    config = _load(args)
    try:
        settings = DatabaseSettings.from_env()
    except DatabaseNotConfigured as exc:
        print(str(exc))
        return EXIT_PROBLEM

    dry_run = not args.live
    fixtures = Path(args.fixtures) if args.fixtures else None
    try:
        with connect(settings.app_dsn) as conn:
            with entity_session(conn, config.entity_id) as scoped:
                summary = stage_supplier_cart(
                    conn=scoped,
                    config=config,
                    supplier=args.supplier,
                    output_dir=Path(args.output),
                    fixtures=fixtures,
                    dry_run=dry_run,
                    week=args.week,
                    now=datetime.now(tz=UTC),
                )
            conn.commit()
            if summary.error is None and not args.no_email:
                with entity_session(conn, config.entity_id) as scoped:
                    summary = deliver_staging_report(conn=scoped, config=config, summary=summary)
                conn.commit()
    except NothingToStage as exc:
        print(str(exc))
        return EXIT_PROBLEM
    except (CartUnavailable, CartRefusal) as exc:
        print(str(exc))
        print("Nothing was added to the cart.")
        return EXIT_PROBLEM

    if summary.error is not None:
        print(f"Nothing was staged: {summary.error}")
        return EXIT_PROBLEM
    verb = "would be added" if summary.dry_run else "added"
    print(f"{summary.staged} line(s) {verb} to the {summary.supplier} cart.")
    if summary.failed:
        print(f"{summary.failed} line(s) could NOT be added — see the report.")
    if summary.plan.skipped:
        print(
            f"{len(summary.plan.skipped)} line(s) have no supplier SKU and must be "
            "ordered by hand — see the report."
        )
    print(f"Report written to {summary.report_path}")
    if summary.email_error is not None:
        print(f"\nThe report was NOT emailed: {summary.email_error}")
        print("The report itself is written and safe, and the failed attempt is recorded.")
        return EXIT_PROBLEM
    if summary.emailed_to:
        print(f"Emailed to {', '.join(summary.emailed_to)} — subject: {summary.email_subject}")
    if summary.dry_run:
        print("This was a dry run: the cart was read and nothing in it was changed.")
    print(
        "Nothing was submitted, no payment was made and no order was placed. "
        "Review the cart and order it yourself."
    )
    return EXIT_OK


def cmd_schedule(args: argparse.Namespace) -> int:
    config = _load(args)
    now = datetime.now(tz=UTC)
    print(f"Schedules for {config.entity.legal_name}, as of {now:%A %d %B %Y %H:%M} UTC")
    for agent in config.entity.agents:
        try:
            due = is_due(agent.schedule, now)
        except ScheduleError as exc:
            print(f"  {agent.kind}: {exc}")
            continue
        print(f"  {agent.kind:<28} {agent.schedule:<24} " + ("due now" if due else "not due"))
    print("")
    print("Runs are started by hand: `shannon run`.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shannon",
        description=(
            "Shannon, the replenishment agent. She reads Veeqo and the inbox, "
            "calculates, writes a report and emails it to the business. She "
            "cannot order, stage a cart or write to a supplier."
        ),
    )
    parser.add_argument("--entity", default="ithrive", help="which business to run for")
    parser.add_argument(
        "--config-root", default="config", help="folder holding the configuration files"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Every command says in one line what it does and what it touches.
    # "What it touches" is the part that matters: the difference between a
    # command that reads files and one that writes to the database should
    # not have to be learned by running it.
    validate_cmd = sub.add_parser(
        "validate-config",
        help=(
            "check the configuration and parts list and print what is wrong. "
            "Reads the config folder only; touches no database and nothing outside "
            "this machine."
        ),
    )
    validate_cmd.add_argument(
        "--verbose", action="store_true", help="also list every configuration value read"
    )
    validate_cmd.add_argument(
        "--as-of",
        default="",
        help=(
            "check the configuration as though today were this day, written "
            "2026-08-26. Only affects the checks that compare against today, such "
            "as whether a hand count is dated in the future"
        ),
    )
    validate_cmd.set_defaults(func=cmd_validate_config)

    migrate_cmd = sub.add_parser(
        "migrate",
        help=(
            "create or update the database tables, and set Shannon's database "
            "password from POSTGRES_APP_PASSWORD. Writes to Postgres only."
        ),
    )
    migrate_cmd.set_defaults(func=cmd_migrate)

    sync_cmd = sub.add_parser(
        "sync-config",
        help=(
            "copy the parts list, suppliers and channels from the config folder "
            "into Postgres. Reads the config folder, writes to Postgres."
        ),
    )
    sync_cmd.set_defaults(func=cmd_sync)

    run_cmd = sub.add_parser(
        "run",
        help=(
            "work out this week's replenishment, write the report and email it to "
            "the addresses this business's configuration names. Reads Veeqo and "
            "the inbox, writes a report file and a row in Postgres, sends one "
            "email. Stages nothing, orders nothing, contacts no supplier."
        ),
    )
    run_cmd.add_argument(
        "--fixtures",
        default="tests/fixtures/golden/data",
        help=(
            "folder of saved Veeqo/Gmail exports to read instead of the live "
            "accounts. Pass an empty value (--fixtures '') to read the live Veeqo "
            "account and the live mailbox, both read-only"
        ),
    )
    run_cmd.add_argument("--output", default="reports", help="folder to write the report file into")
    run_cmd.add_argument(
        "--no-email",
        action="store_true",
        help=(
            "write the report but do not email it. For trying a change out "
            "without putting anything in an inbox"
        ),
    )
    run_cmd.add_argument(
        "--again",
        action="store_true",
        help=(
            "work this week out afresh even though it has already been COMPLETED. "
            "The new report replaces the previous one for the week; both are kept "
            "in the database and the old file is renamed rather than overwritten. "
            "You do not need this flag after a run that FAILED — nothing was "
            "completed then, so plain `shannon run` picks that week up again. It "
            "re-reads, re-reports and re-sends the report by email: nothing is "
            "re-staged, re-ordered or bought, and no supplier is contacted. Use it "
            "as often as you like — if all you need is the email, `shannon resend` "
            "sends the report that already exists without reading anything."
        ),
    )
    run_cmd.set_defaults(func=cmd_run)

    resend_cmd = sub.add_parser(
        "resend",
        help=(
            "email a report that is already written, without working the week out "
            "again. For when the report is right and the mail server was not. "
            "Reads nothing from Veeqo or the inbox, writes no new report, and "
            "records the attempt beside the earlier ones."
        ),
    )
    resend_cmd.add_argument(
        "--week",
        default=None,
        help=(
            "the ISO week to send, as 2026-W35. Defaults to the week it is now. "
            "The report sent is the one that currently stands for that week; a "
            "report a re-run has superseded is never sent."
        ),
    )
    resend_cmd.set_defaults(func=cmd_resend)

    stage_cmd = sub.add_parser(
        "stage",
        help=(
            "put the lines this week's report routes to a supplier's cart into "
            "that cart, and email a confirmation. Reads the report already in the "
            "database — it works nothing out again. It never checks out, never "
            "pays and never places an order, at any tier. Without --live it is a "
            "dry run: the cart is read and nothing in it is changed."
        ),
    )
    stage_cmd.add_argument(
        "--supplier", default="nar", help="which supplier's cart to stage (nar today)"
    )
    stage_cmd.add_argument(
        "--week",
        default=None,
        help="the ISO week to stage, as 2026-W35. Defaults to the week it is now",
    )
    stage_cmd.add_argument(
        "--fixtures",
        default="tests/fixtures/golden/data",
        help=(
            "folder holding a saved copy of the cart to read instead of the live "
            "site. Pass an empty value (--fixtures '') to read the real cart, "
            "which needs the supplier login in the environment"
        ),
    )
    stage_cmd.add_argument(
        "--output", default="reports", help="folder to write the confirmation report into"
    )
    stage_cmd.add_argument(
        "--no-email", action="store_true", help="write the confirmation report but do not email it"
    )
    stage_cmd.add_argument(
        "--live",
        action="store_true",
        help=(
            "actually add the lines to the supplier's cart instead of rehearsing "
            "it. Refused while the phase ceiling in config/policy/global.yaml is "
            "0, which it is. Even then it stages only: no checkout, no payment, "
            "no order"
        ),
    )
    stage_cmd.set_defaults(func=cmd_stage)

    schedule_cmd = sub.add_parser(
        "schedule",
        help=(
            "show which runs are due and when. Reads the config folder and the "
            "clock; changes nothing anywhere."
        ),
    )
    schedule_cmd.set_defaults(func=cmd_schedule)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Before anything resolves configuration: `docker compose` reads .env, so
    # Shannon reads the same file. Anything already in the real environment
    # wins, so a value can be overridden for one command without editing it.
    load_env_file()
    args = build_parser().parse_args(argv)
    try:
        exit_code: int = args.func(args)
    except ConfigError as exc:
        for finding in exc.findings:
            print(finding.render())
        return EXIT_PROBLEM
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
