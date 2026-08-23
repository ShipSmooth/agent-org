"""Plain-English configuration findings.

Zach is not an engineer and will not read a stack trace. Every problem the
config checks find is a sentence naming the file, the line, what is wrong,
and what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent_org.config.yamlsource import Loc


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    message: str
    loc: Loc
    fix: str | None = None
    # Some errors are fatal to the whole run (a parameter that cannot be
    # read); others spoil one line or one kit and nothing else — a
    # component reference pointing at nothing, a channel SKU still marked
    # TODO. `shannon validate-config` fails on both. A run stops on the
    # first kind and carries on for the second, reporting the affected
    # lines as blocked, because a report about the other forty components
    # is worth having.
    blocks_run: bool = True

    def render(self) -> str:
        label = "ERROR  " if self.severity is Severity.ERROR else "WARNING"
        lines = [f"{label} {self.loc}", f"        {self.message}"]
        if self.fix:
            lines.append(f"        What to do: {self.fix}")
        return "\n".join(lines)


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid.

    Carries findings rather than a traceback; the CLI prints them and exits
    non-zero.
    """

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        super().__init__("\n".join(finding.render() for finding in findings))


def error(message: str, loc: Loc, fix: str | None = None, blocks_run: bool = True) -> Finding:
    return Finding(
        severity=Severity.ERROR,
        message=message,
        loc=loc,
        fix=fix,
        blocks_run=blocks_run,
    )


def warning(message: str, loc: Loc, fix: str | None = None) -> Finding:
    return Finding(severity=Severity.WARNING, message=message, loc=loc, fix=fix, blocks_run=False)
