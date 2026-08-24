"""Writing the report down, and handing it to its owner.

Two executors, and between them the only effects Shannon has:

* `internal.write_draft_report` writes a text file Zach can open and a row
  in the database, so a report cannot be lost by someone tidying a folder.
* `internal.email_report_to_owner` emails a report that is already written
  to the roles that entity's configuration names. It reads the body out of
  the database rather than being handed one, so what arrives in the inbox
  is provably the thing that was filed.

Neither one can reach a supplier, stage a cart or buy anything. Those
executors do not exist.

Durability, which was four loose steps in Phase 1 and is now ordered:

1. The new report is written to a temporary file beside its final name.
   Half a file never appears under a name Zach might open.
2. The database insert and the supersession of last week's row happen in
   one transaction. They both land or neither does.
3. Only then is the temporary file renamed into place, which on every
   filesystem this runs on is atomic.
4. The email is sent afterwards, by a separate action, once the report is
   committed to both the database and the disk.

A crash between 3 and the commit leaves a file with no row. That case is
handled rather than ignored: an existing file is always moved aside before
a new one lands, so an orphan is preserved rather than overwritten, and
the next run reports the collision instead of quietly winning.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg

from agent_org.broker.registry import Executor, ExecutorRegistry
from agent_org.notify.email import Mail, Sender, SendFailed

ACTION_WRITE_REPORT = "internal.write_draft_report"
ACTION_EMAIL_REPORT = "internal.email_report_to_owner"


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

        temporary = self._write_temporary(path, body)
        try:
            # One transaction: the new row and the mark on the old one land
            # together or not at all. Nested inside the broker's own
            # transaction this is a savepoint, which is the same guarantee
            # from the point of view of anything reading the table.
            with self.conn.transaction():
                previous = self._current_report(task_id)
                report_id = self._insert(task_id, payload, body, path)
                kept = (
                    self._keep_the_old_file(path, previous.created_at)
                    if previous is not None
                    else self._keep_an_orphan_file(path)
                )
                if previous is not None:
                    self._mark_superseded(previous.report_id, report_id, kept)
                self._record_manual_proposals(payload, report_id)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        result: dict[str, Any] = {
            "report_id": report_id,
            "file_path": str(path),
            "bytes": len(body),
        }
        if previous is not None:
            result["supersedes"] = previous.report_id
            result["supersedes_written_at"] = previous.created_at.isoformat()
            result["supersedes_file_path"] = kept
        elif kept is not None:
            result["kept_orphan_file"] = kept
        return result

    def _write_temporary(self, path: Path, body: str) -> Path:
        """The new report, written where nobody will mistake it for one.

        Written and flushed to the disk before anything is renamed, so the
        atomic step that follows moves a file that is known to be complete.
        """
        temporary = path.with_name(f".{path.name}.writing-{uuid.uuid4().hex}")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary

    def _insert(self, task_id: str, payload: dict[str, Any], body: str, path: Path) -> str:
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
            return str(row[0])

    def _record_manual_proposals(self, payload: dict[str, Any], report_id: str) -> None:
        """Remember what was proposed against each hand count.

        A hand count does not fall when the stock is used, so without this
        the same eleven components would be proposed every Monday until
        somebody recounted them. The row is keyed on the count date: an
        unchanged count is suppressed next week and on a --again, and a
        genuinely new count proposes again.
        """
        proposals = payload.get("manual_proposals", [])
        if not isinstance(proposals, list):
            return
        for entry in proposals:
            if not isinstance(entry, dict):
                continue
            self.conn.execute(
                """
                INSERT INTO manual_stock_proposals (entity_id, supplier, part, counted_on,
                                                    count_units, proposed_units, report_id,
                                                    proposed_on)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, supplier, part, counted_on) DO NOTHING
                """,
                (
                    self.entity_id,
                    str(entry["supplier"]),
                    str(entry["part"]),
                    date.fromisoformat(str(entry["counted_on"])),
                    int(entry["count"]),
                    int(entry["units"]),
                    report_id,
                    date.fromisoformat(str(entry["proposed_on"])),
                ),
            )

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

    def _keep_an_orphan_file(self, path: Path) -> str | None:
        """A file with no row behind it is kept, not overwritten.

        That combination means a previous run was interrupted between
        renaming its file into place and committing its row. It is rare and
        it is real, and the copy on disk may be the only record of what that
        run said.
        """
        if not path.exists():
            return None
        kept = self._kept_path(path, datetime.fromtimestamp(path.stat().st_mtime))
        path.rename(kept)
        return str(kept)


@dataclass
class ReportEmailer:
    """Hand a written report to the people that entity's config names.

    The body is read back out of the database by id. Nothing about the
    message is taken on trust from the caller except which report and which
    addresses, and the addresses came from configured roles, which
    validate-config has already refused to let point outside the business.
    """

    conn: psycopg.Connection[tuple[object, ...]]
    entity_id: str
    sender: Sender
    from_name: str
    from_address: str

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        report_id = str(payload["report_id"])
        recipients = tuple(str(address) for address in payload.get("recipients", []))
        subject = str(payload["subject"])
        if not recipients:
            raise SendFailed(
                "The report is written, but no recipient is configured for it, so it "
                "has been emailed to nobody. Add a role under 'reports: email_to:'."
            )
        body = self._body_of(report_id)
        mail = Mail(
            from_name=self.from_name,
            from_address=self.from_address,
            to=recipients,
            subject=subject,
            body=body,
        )
        try:
            server = self.sender.send(mail)
        except SendFailed as exc:
            self._record(report_id, recipients, subject, "FAILED", str(exc))
            raise
        self._record(report_id, recipients, subject, "SENT", None)
        return {
            "report_id": report_id,
            "recipients": list(recipients),
            "subject": subject,
            "server": server,
        }

    def _body_of(self, report_id: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT body FROM reports WHERE entity_id = %s AND id = %s",
                (self.entity_id, report_id),
            )
            row = cur.fetchone()
        if row is None:
            raise SendFailed(f"There is no report {report_id} to send. Nothing has been emailed.")
        return str(row[0])

    def _record(
        self,
        report_id: str,
        recipients: tuple[str, ...],
        subject: str,
        status: str,
        error: str | None,
    ) -> None:
        """Every attempt is recorded, including the ones that failed.

        Kept apart from the report row so that "what did this week say" and
        "did it reach him" stay separate questions — and so a resend is a
        second row here rather than something that looks like a second run.
        """
        self.conn.execute(
            """
            INSERT INTO report_emails (entity_id, report_id, recipients, subject,
                                       status, error)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (self.entity_id, report_id, ", ".join(recipients), subject, status, error),
        )


def report_email_executor(emailer: ReportEmailer) -> Executor:
    """The one thing Shannon sends, described for the broker.

    Not reversible — an email cannot be recalled — and Tier 0 all the same,
    because it goes to the operator this system reports to and carries only
    the report he asked for. `config/policy/global.yaml` says why in full.
    """
    return Executor(
        action_type=ACTION_EMAIL_REPORT,
        reversible="no",
        category="internal",
        supplier=None,
        requires_capability=None,
        run=emailer,
    )


def build_registry(writer: ReportWriter, emailer: ReportEmailer | None = None) -> ExecutorRegistry:
    """What Shannon is able to do, in full.

    Writing a report, and — in this phase, newly — emailing that report to
    its owner. There is still no executor for staging a cart, writing to a
    supplier or buying anything: those are absent, not switched off, and
    adding one is a visible line in a diff.
    """
    registry = ExecutorRegistry()
    registry.register(
        Executor(
            action_type=ACTION_WRITE_REPORT,
            reversible="yes",
            category="internal",
            supplier=None,
            requires_capability=None,
            run=writer,
        )
    )
    if emailer is not None:
        registry.register(report_email_executor(emailer))
    return registry


__all__ = [
    "ACTION_EMAIL_REPORT",
    "ACTION_WRITE_REPORT",
    "PreviousReport",
    "ReportEmailer",
    "ReportWriter",
    "build_registry",
    "report_email_executor",
]
