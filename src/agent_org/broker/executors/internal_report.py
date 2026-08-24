"""The only executor that exists in Phase 1: write the report down.

It writes two copies — a text file Zach can open, and a row in the
database so the report cannot be lost and the next run can say what
changed. It sends nothing anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from agent_org.broker.registry import Executor, ExecutorRegistry


@dataclass(frozen=True)
class PreviousReport:
    report_id: str
    created_at: datetime


@dataclass
class ReportWriter:
    conn: psycopg.Connection[tuple[object, ...]]
    entity_id: str
    output_dir: Path

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = str(payload["body"])
        filename = str(payload["filename"])
        task_id = str(payload["task_id"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / filename

        # A re-run of a week that already has a report replaces it. Both rows
        # stay: the old one is never edited beyond being marked, so "why does
        # this week have two reports" is answerable from the data alone.
        previous = self._current_report(task_id)
        kept = self._keep_the_old_file(path, previous.created_at) if previous is not None else None
        path.write_text(body, encoding="utf-8")

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reports (entity_id, task_id, agent_kind, kind, bom_version,
                                     config_digest, parameters, lines, body, file_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.entity_id,
                    task_id,
                    "shannon",
                    str(payload.get("kind", "replenishment")),
                    str(payload["bom_version"]),
                    str(payload["config_digest"]),
                    json.dumps(payload.get("parameters", {}), default=str),
                    json.dumps(payload.get("lines", []), default=str),
                    body,
                    str(path),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            report_id = str(row[0])

        result: dict[str, Any] = {
            "report_id": report_id,
            "file_path": str(path),
            "bytes": len(body),
        }
        if previous is not None:
            self._mark_superseded(previous.report_id, report_id, kept)
            result["supersedes"] = previous.report_id
            result["supersedes_written_at"] = previous.created_at.isoformat()
            result["supersedes_file_path"] = kept
        return result

    def _current_report(self, task_id: str) -> PreviousReport | None:
        """The report this run replaces, if this week has already been run."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at FROM reports
                 WHERE entity_id = %s AND task_id = %s AND superseded_by IS NULL
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (self.entity_id, task_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        created_at = row[1]
        assert isinstance(created_at, datetime)
        return PreviousReport(report_id=str(row[0]), created_at=created_at)

    def _mark_superseded(self, old_id: str, new_id: str, kept: str | None) -> None:
        """Mark the old row, and point it at the file it now lives in.

        Leaving `file_path` on the old row would have two rows claiming one
        filename, and the older of them would be wrong.
        """
        self.conn.execute(
            """
            UPDATE reports
               SET superseded_by = %s,
                   superseded_at = now(),
                   file_path = COALESCE(%s, file_path)
             WHERE id = %s
            """,
            (new_id, kept, old_id),
        )

    @staticmethod
    def _kept_path(path: Path, written_at: datetime) -> Path:
        """A name of its own for the replaced report.

        The stamp is to the second, and two re-runs inside one second would
        otherwise land on the same name and lose the earlier report, so a
        counter is added rather than overwriting.
        """
        stamp = f"{written_at:%Y%m%dT%H%M%SZ}"
        kept = path.with_name(f"{path.stem}.superseded-{stamp}{path.suffix}")
        attempt = 2
        while kept.exists():
            kept = path.with_name(f"{path.stem}.superseded-{stamp}-{attempt}{path.suffix}")
            attempt += 1
        return kept

    def _keep_the_old_file(self, path: Path, written_at: datetime) -> str | None:
        """The replaced report keeps a filename of its own.

        Zach reads the file, not the table, and a re-run that silently
        overwrote the file would leave the database honest and the folder
        misleading.
        """
        if not path.exists():
            return None
        kept = self._kept_path(path, written_at)
        path.rename(kept)
        return str(kept)


def build_registry(writer: ReportWriter) -> ExecutorRegistry:
    """The Phase 1 registry: writing a report, and nothing else.

    Every later phase adds executors here — staging carts, sending the
    report — and each addition is a visible line in a diff.
    """
    registry = ExecutorRegistry()
    registry.register(
        Executor(
            action_type="internal.write_draft_report",
            reversible="yes",
            category="internal",
            supplier=None,
            requires_capability=None,
            run=writer,
        )
    )
    return registry


__all__ = ["PreviousReport", "ReportWriter", "build_registry"]
