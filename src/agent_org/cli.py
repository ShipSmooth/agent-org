"""The `shannon` command.

Everything it prints is meant to be read by someone who is not an
engineer. No tracebacks: a failure is a sentence saying what went wrong,
in which file, and what to do about it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
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
from agent_org.integrations.reads import ReadFailure
from agent_org.runtime.worker import RunAlreadyDone, run_replenishment
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

    result = validate(config, findings)
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

    try:
        with connect(settings.app_dsn) as conn:
            with entity_session(conn, config.entity_id) as scoped:
                summary = run_replenishment(
                    conn=scoped,
                    config=config,
                    fixtures=Path(args.fixtures),
                    output_dir=Path(args.output),
                    now=datetime.now(tz=UTC),
                )
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
    print(
        "Nothing was ordered and nothing was sent. Read the report, then place any orders yourself."
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
    print("Phase 1 runs are started by hand: `shannon run`.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shannon",
        description=(
            "Shannon, the replenishment agent. In this phase she reads, calculates "
            "and writes a report; she cannot order, send or change anything."
        ),
    )
    parser.add_argument("--entity", default="ithrive", help="which business to run for")
    parser.add_argument(
        "--config-root", default="config", help="folder holding the configuration files"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate_cmd = sub.add_parser("validate-config", help="check the configuration and parts list")
    validate_cmd.add_argument("--verbose", action="store_true")
    validate_cmd.set_defaults(func=cmd_validate_config)

    migrate_cmd = sub.add_parser("migrate", help="create or update the database tables")
    migrate_cmd.set_defaults(func=cmd_migrate)

    sync_cmd = sub.add_parser("sync-config", help="copy the configuration into the database")
    sync_cmd.set_defaults(func=cmd_sync)

    run_cmd = sub.add_parser("run", help="run this week's replenishment and write a report")
    run_cmd.add_argument(
        "--fixtures",
        default="tests/fixtures/golden/data",
        help="folder of saved Veeqo/Gmail exports to read instead of live accounts",
    )
    run_cmd.add_argument("--output", default="reports", help="where to write the report")
    run_cmd.set_defaults(func=cmd_run)

    schedule_cmd = sub.add_parser("schedule", help="show what is scheduled and when")
    schedule_cmd.set_defaults(func=cmd_schedule)
    return parser


def main(argv: list[str] | None = None) -> int:
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
