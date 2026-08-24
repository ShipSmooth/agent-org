"""The scheduler — present and wired; the manual trigger matters in Phase 1.

Shannon's weekly run is Monday 06:00 in the entity's timezone
(docs/agents.md). A slot is named by its ISO week (``2026-W34``); the
task table's uniqueness on (entity, kind, slot) means enqueueing the same
slot twice is a no-op, so a crashed scheduler can simply run again.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from agent_org.runtime.tasks import enqueue

WEEKLY_KIND = "shannon.replenishment"
RUN_WEEKDAY = 0  # Monday
RUN_HOUR = 6


def slot_for(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def next_run_at(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    target = local.replace(hour=RUN_HOUR, minute=0, second=0, microsecond=0)
    days_ahead = (RUN_WEEKDAY - local.weekday()) % 7
    target = target + timedelta(days=days_ahead)
    if target <= local:
        target += timedelta(days=7)
    return target


def tick(conn: psycopg.Connection, entity_id: str, tz: str, now: datetime) -> str | None:
    """Enqueue this week's run if its time has come. Returns the task id or None."""
    local = now.astimezone(ZoneInfo(tz))
    due = local.replace(hour=RUN_HOUR, minute=0, second=0, microsecond=0) - timedelta(
        days=local.weekday()
    )
    if local < due:
        return None
    task_id = enqueue(conn, entity_id, WEEKLY_KIND, slot_for(local))
    conn.commit()
    return task_id
