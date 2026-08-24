"""The only executor that exists in Phase 1: write the report down.

It writes two copies — a text file Zach can open, and a row in the
database so the report cannot be lost and the next run can say what
changed. It sends nothing anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from agent_org.broker.registry import Executor, ExecutorRegistry


@dataclass
class ReportWriter:
    conn: psycopg.Connection[tuple[object, ...]]
    entity_id: str
    output_dir: Path

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = str(payload["body"])
        filename = str(payload["filename"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / filename
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
                    str(payload["task_id"]),
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
        return {"report_id": report_id, "file_path": str(path), "bytes": len(body)}


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


__all__ = ["ReportWriter", "build_registry"]
