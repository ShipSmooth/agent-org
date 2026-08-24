"""Reading the schedules out of the entity file.

Only the two forms the configuration actually uses are supported:

    schedule: "cron: 0 6 * * MON"     — a day of the week and a time
    schedule: "every: 6 weeks"        — a fixed interval

A general cron parser would be more code and more ways to be wrong, and
nothing here needs one. Anything else is rejected by name so a typo is
never silently ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DAYS = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}

CRON = re.compile(r"^cron:\s*(\d+)\s+(\d+)\s+\*\s+\*\s+([A-Z]{3})$")
EVERY = re.compile(r"^every:\s*(\d+)\s+weeks?$")


class ScheduleError(ValueError):
    """The schedule line in the entity file cannot be understood."""


@dataclass(frozen=True)
class WeeklySchedule:
    weekday: int
    hour: int
    minute: int

    def is_due(self, now: datetime, last_run: datetime | None = None) -> bool:
        """Due from its moment on, until that week's run has happened.

        A missed Monday — the Dell rebooting, a power cut — must not mean a
        skipped week. The run is keyed to the ISO week, so catching up on
        the Wednesday runs it once, not twice.
        """
        if now.weekday() < self.weekday:
            return False
        if now.weekday() == self.weekday and (now.hour, now.minute) < (self.hour, self.minute):
            return False
        if last_run is None:
            return True
        return last_run.isocalendar()[:2] != now.isocalendar()[:2]


@dataclass(frozen=True)
class IntervalSchedule:
    weeks: int

    def is_due(self, now: datetime, last_run: datetime | None = None) -> bool:
        if last_run is None:
            return True
        return now - last_run >= timedelta(weeks=self.weeks)


Schedule = WeeklySchedule | IntervalSchedule


def parse(expression: str) -> Schedule:
    text = expression.strip()
    cron = CRON.match(text)
    if cron:
        minute, hour, day = cron.groups()
        if day not in DAYS:
            raise ScheduleError(f"'{day}' is not a day of the week. Use one of: {', '.join(DAYS)}.")
        return WeeklySchedule(weekday=DAYS[day], hour=int(hour), minute=int(minute))
    every = EVERY.match(text)
    if every:
        return IntervalSchedule(weeks=int(every.group(1)))
    raise ScheduleError(
        f"Cannot understand the schedule '{expression}'. Write it either as "
        "'cron: 0 6 * * MON' or as 'every: 6 weeks'."
    )


def is_due(expression: str, now: datetime | None = None, last_run: datetime | None = None) -> bool:
    return parse(expression).is_due(now or datetime.now(tz=UTC), last_run)


__all__ = [
    "IntervalSchedule",
    "Schedule",
    "ScheduleError",
    "WeeklySchedule",
    "is_due",
    "parse",
]
