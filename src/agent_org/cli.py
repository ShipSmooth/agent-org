"""The `shannon` command line.

    shannon validate-config [--config DIR] [--entity ithrive]
    shannon migrate
    shannon run --fixtures DIR [--out DIR] [--slot SLOT] [--policy DIR]  (manual)
    shannon schedule-tick                                   (cron entrypoint)

Errors are plain English; there are no stack traces on bad config or bad
data — a non-zero exit and a sentence saying what to fix.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from agent_org.db.connection import MissingDatabaseUrlError, connect
from agent_org.db.migrate import migrate
from agent_org.integrations.gmail import GmailReadError
from agent_org.integrations.veeqo import VeeqoReadError
from agent_org.runtime.budgets import BudgetExceededError
from agent_org.runtime.wiring import build_broker
from agent_org.scheduler.schedule import next_run_at, slot_for, tick
from agent_org.shannon.calculator import RunStopped
from agent_org.shannon.config_model import load_entity_config
from agent_org.shannon.configload import ConfigError
from agent_org.shannon.run import run_replenishment
from agent_org.shannon.validate import validate


def _validate_config(config_dir: Path, entity_id: str) -> int:
    try:
        cfg = load_entity_config(config_dir, entity_id)
        issues = validate(cfg)
    except ConfigError as exc:
        for issue in exc.issues:
            print(issue.render())
        print("\nvalidate-config: FAILED — fix the problems above and run again.")
        return 1
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    print(f"bom_version: {cfg.bom_version}")
    for issue in [*errors, *warnings]:
        print(issue.render())
    if errors:
        print(
            f"\nvalidate-config: FAILED — {len(errors)} error(s), "
            f"{len(warnings)} warning(s). Every error above names its file and line."
        )
        return 1
    print(
        f"\nvalidate-config: OK — 0 errors, {len(warnings)} warning(s). "
        "Shannon can run against this configuration."
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    config_dir = Path(args.config)
    try:
        cfg = load_entity_config(config_dir, args.entity)
        broker = build_broker(cfg, Path(args.policy))
        slot = args.slot or f"{slot_for(datetime.now(tz=UTC))}-manual"
        with connect() as conn:
            outcome = run_replenishment(
                conn,
                broker,
                config_dir=config_dir,
                entity_id=args.entity,
                fixtures_dir=Path(args.fixtures),
                out_dir=Path(args.out),
                schedule_slot=slot,
            )
    except (
        ConfigError,
        RunStopped,
        VeeqoReadError,
        GmailReadError,
        BudgetExceededError,
        MissingDatabaseUrlError,
    ) as exc:
        print(f"Run stopped: {exc}")
        return 1
    print(f"Run complete. Report: {outcome.report_path}")
    return 0


def _schedule_tick(args: argparse.Namespace) -> int:
    try:
        cfg = load_entity_config(Path(args.config), args.entity)
        now = datetime.now(tz=UTC)
        with connect() as conn:
            task_id = tick(conn, args.entity, cfg.timezone, now)
    except (ConfigError, MissingDatabaseUrlError) as exc:
        print(f"Scheduler stopped: {exc}")
        return 1
    if task_id:
        print(f"Enqueued this week's run (task {task_id}).")
    else:
        print(f"Nothing due. Next run: {next_run_at(now, cfg.timezone):%A %Y-%m-%d %H:%M %Z}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="shannon", description="Shannon — the iThrive Medical replenishment agent."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate-config", help="Check the config; plain-English errors.")
    p_val.add_argument("--config", default="config")
    p_val.add_argument("--entity", default="ithrive")

    p_mig = sub.add_parser("migrate", help="Apply database migrations.")
    p_mig.add_argument("--quiet", action="store_true")

    p_run = sub.add_parser("run", help="Run the weekly replenishment now (manual trigger).")
    p_run.add_argument("--config", default="config")
    p_run.add_argument("--entity", default="ithrive")
    p_run.add_argument(
        "--policy",
        default="config",
        help="Where the policy rules live (default: config). A fixture run still "
        "obeys the real policy; a directory with no policy file denies everything.",
    )
    p_run.add_argument("--fixtures", required=True, help="Directory of data files to read.")
    p_run.add_argument("--out", default="reports", help="Where the report file lands.")
    p_run.add_argument("--slot", default=None, help="Schedule slot name (default: this week).")

    p_tick = sub.add_parser("schedule-tick", help="Enqueue the weekly run if due (cron).")
    p_tick.add_argument("--config", default="config")
    p_tick.add_argument("--entity", default="ithrive")

    args = parser.parse_args(argv)
    if args.command == "validate-config":
        return _validate_config(Path(args.config), args.entity)
    if args.command == "migrate":
        try:
            applied = migrate()
        except MissingDatabaseUrlError as exc:
            print(f"Cannot migrate: {exc}")
            return 1
        if not args.quiet:
            print(f"Applied: {', '.join(applied) if applied else 'nothing — up to date'}.")
        return 0
    if args.command == "run":
        return _run(args)
    return _schedule_tick(args)


if __name__ == "__main__":
    sys.exit(main())
