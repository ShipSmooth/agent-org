"""Run the live parts list twice, a week apart, against saved exports.

Not a test: a way to look at two consecutive Mondays by hand, which is the
only way to see what the second week says about a hand count that has not
changed. Needs a database (`docker compose up -d`, then `shannon migrate`).

    uv run python scripts/two_weeks_demo.py <output-dir>
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_org.config.loader import load_config
from agent_org.db.connection import DatabaseSettings, connect, entity_session
from agent_org.notify.email import RecordingSender
from agent_org.runtime.worker import deliver_report, run_replenishment
from agent_org.tenancy.registry import register_entity

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "ithrive-sample"
WEEK_ONE = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
WEEK_TWO = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)


def main(output: Path) -> int:
    config, _ = load_config(REPO / "config", "ithrive")
    settings = DatabaseSettings.from_env()
    with connect(settings.migrator_dsn) as owner:
        register_entity(owner, config.entity)
        owner.commit()

    sender = RecordingSender()
    with connect(settings.app_dsn) as conn:
        for moment in (WEEK_ONE, WEEK_TWO):
            with entity_session(conn, config.entity_id) as scoped:
                summary = run_replenishment(
                    conn=scoped,
                    config=config,
                    fixtures=FIXTURES,
                    output_dir=output,
                    now=moment,
                )
            conn.commit()
            if summary.error is not None:
                print(f"{moment:%d %b}: the run stopped: {summary.error}")
                return 1
            with entity_session(conn, config.entity_id) as scoped:
                summary = deliver_report(
                    conn=scoped, config=config, summary=summary, sender=sender
                )
            conn.commit()
            print(f"{moment:%d %b}: {summary.report_path}")
            print(f"{moment:%d %b}: emailed to {summary.emailed_to} — {summary.email_subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "reports")))
