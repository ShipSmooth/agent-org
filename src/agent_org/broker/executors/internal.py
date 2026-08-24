"""Internal executors — effects that stay inside the system.

``write_draft_report`` writes the finished report to a file and to the
``reports`` table. It sends nothing anywhere.
"""

from __future__ import annotations

from pathlib import Path

import psycopg


def write_draft_report(
    conn: psycopg.Connection, entity_id: str, payload: dict[str, object]
) -> dict[str, object]:
    content = str(payload["content"])
    out_dir = Path(str(payload["out_dir"]))
    schedule_slot = str(payload["schedule_slot"])
    file_name = f"shannon-{entity_id}-{schedule_slot.replace('/', '-')}.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / file_name
    file_path.write_text(content, encoding="utf-8")
    conn.execute(
        """
        INSERT INTO reports
            (entity_id, task_id, kind, schedule_slot, bom_version,
             config_snapshot, content, file_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            entity_id,
            str(payload["task_id"]),
            str(payload["kind"]),
            schedule_slot,
            str(payload["bom_version"]),
            str(payload["config_snapshot"]),
            content,
            str(file_path),
        ),
    )
    return {"file_path": str(file_path)}
