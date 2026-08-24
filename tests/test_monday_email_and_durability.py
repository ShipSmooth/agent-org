"""The Monday email, and the order the five steps happen in.

Phase 1 wrote a file and a row in four loose steps. This phase adds a
fifth step that leaves the machine — an email — so the order matters in a
way it did not before: the database work is one transaction, the file
appears under its final name only when it is complete, and the send comes
after both, recorded where a resend can never be mistaken for a re-run.

Every send here goes to a recorder. No mail server is contacted and no
credential exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from agent_org.config.models import LoadedConfig
from agent_org.db.connection import entity_session
from agent_org.notify.email import RecordingSender, SendFailed, subject_line
from agent_org.runtime.worker import (
    SHANNON_REPLENISHMENT,
    RunAlreadyDone,
    RunSummary,
    deliver_report,
    run_replenishment,
)

DATA = Path(__file__).parent / "fixtures" / "golden" / "data"
MONDAY = datetime(2026, 3, 30, 6, 0, tzinfo=UTC)
NEXT_MONDAY = datetime(2026, 4, 6, 6, 0, tzinfo=UTC)


def _run_and_send(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    output_dir: Path,
    sender: RecordingSender,
    when: datetime = MONDAY,
    again: bool = False,
) -> RunSummary:
    summary = run_replenishment(
        conn=conn,
        config=config,
        fixtures=DATA,
        output_dir=output_dir,
        now=when,
        again=again,
    )
    return deliver_report(conn=conn, config=config, summary=summary, sender=sender)


def _rows(conn: psycopg.Connection[tuple[object, ...]], sql: str) -> list[tuple[object, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


# --- the subject line, which is what he actually reads --------------------


def test_the_subject_carries_the_week_and_the_headline() -> None:
    """He reads this on a phone with the laptop shut. The count and whether
    anything is stuck cannot be inside the body."""
    assert subject_line("2026-W14", 9, 0) == "Shannon — week of 2026-W14 — 9 lines to order"
    assert (
        subject_line("2026-W14", 3, 2)
        == "Shannon — week of 2026-W14 — 3 lines to order, 2 lines blocked"
    )
    assert (
        subject_line("2026-W14", 1, 1)
        == "Shannon — week of 2026-W14 — 1 line to order, 1 line blocked"
    )
    assert subject_line("2026-W14", 0, 0) == "Shannon — week of 2026-W14 — nothing to order"


def test_a_quiet_week_still_says_so_rather_than_going_unsent() -> None:
    """ "Nothing to order" is a result. Silence is indistinguishable from a
    crashed machine."""
    assert "nothing to order" in subject_line("2026-W14", 0, 0)


# --- who it goes to -------------------------------------------------------


def test_the_report_goes_to_the_configured_role_and_nowhere_else(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Addresses live in `config/<entity>/shannon.yaml` under role names.
    Nothing here knows an address; it asks the configuration for one."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        summary = _run_and_send(conn, golden_config, tmp_path, sender)
    assert summary.email_error is None, summary.email_error
    assert len(sender.sent) == 1
    mail = sender.sent[0]
    assert mail.to == ("zach@ithrivemedical.com",)
    assert mail.from_address.endswith("@ithrivemedical.com")
    assert "shipsmooth.com" not in " ".join(mail.to) + mail.from_address


def test_no_operational_address_is_written_in_source() -> None:
    """iThrive's address in code would be iThrive's address in Lima Zulu's
    run too. Roles resolve per entity; source knows none of them."""
    source = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path
        for path in source.rglob("*.py")
        if "@ithrivemedical.com" in path.read_text(encoding="utf-8")
        or "@shipsmooth.com" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_the_email_body_is_the_report_that_was_filed(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Read back out of the database by id, so what arrived in the inbox
    and what is in the folder cannot drift apart."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        summary = _run_and_send(conn, golden_config, tmp_path, sender)
    assert summary.report_path is not None
    on_disk = Path(summary.report_path).read_text(encoding="utf-8")
    assert sender.sent[0].body == on_disk


def test_shannon_sends_nothing_else(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """One message, to the operator. No supplier mail, no reply, no
    forward — and the report says as much in its own words."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        summary = _run_and_send(conn, golden_config, tmp_path, sender)
        actions = _rows(
            conn,
            "SELECT action_type FROM action_proposals WHERE entity_id = 'ithrive'",
        )
    assert len(sender.sent) == 1
    assert {str(row[0]) for row in actions} <= {
        "internal.write_draft_report",
        "internal.email_report_to_owner",
    }
    assert summary.report_path is not None
    body = Path(summary.report_path).read_text(encoding="utf-8")
    assert "no supplier heard from her" in body
    assert "no email was replied to or forwarded" in body


# --- what happens when the mail server has a bad minute -------------------


def test_a_failed_send_leaves_the_report_written_and_says_so_loudly(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """A week's work must not be lost because SMTP was unavailable for a
    minute. The file stays, the row stays, the failure is reported."""
    sender = RecordingSender(fail_with="the mail server refused the connection")
    with entity_session(app_conn, entity_id) as conn:
        summary = _run_and_send(conn, golden_config, tmp_path, sender)
        emails = _rows(
            conn,
            "SELECT status, error FROM report_emails WHERE entity_id = 'ithrive'",
        )
        reports = _rows(conn, "SELECT count(*) FROM reports WHERE entity_id = 'ithrive'")
    assert summary.email_error is not None
    assert "refused the connection" in summary.email_error
    assert summary.report_path is not None and Path(summary.report_path).exists()
    assert reports[0][0] == 1, "the report is filed whatever the mail server did"
    assert [str(row[0]) for row in emails] == ["FAILED"]
    assert "refused the connection" in str(emails[0][1])


def test_a_failed_send_does_not_make_the_week_re_runnable_by_accident(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The run completed; only the delivery failed. Running again without
    saying `--again` would regenerate a report that already exists."""
    sender = RecordingSender(fail_with="the mail server refused the connection")
    with entity_session(app_conn, entity_id) as conn:
        _run_and_send(conn, golden_config, tmp_path, sender)
        with pytest.raises(RunAlreadyDone):
            run_replenishment(
                conn=conn,
                config=golden_config,
                fixtures=DATA,
                output_dir=tmp_path,
                now=MONDAY,
            )


def test_missing_smtp_settings_are_a_named_failure_not_a_silent_one() -> None:
    """No credential exists in this environment, which is the point: the
    failure names the variable rather than looking like a quiet week."""
    from agent_org.notify.email import Mail, SmtpSender

    sender = SmtpSender(credentials_prefix="ITHRIVE_")
    with pytest.raises(SendFailed) as raised:
        sender.send(
            Mail(
                from_name="Shannon",
                from_address="shannon@example.invalid",
                to=("someone@example.invalid",),
                subject="week of nothing",
                body="body",
            )
        )
    assert "ITHRIVE_SMTP_HOST" in str(raised.value)
    assert "the report is on disk and in the database" in str(raised.value)


# --- sending is recorded apart from running -------------------------------


def test_a_send_is_recorded_against_the_report_not_as_a_run(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """ "What did this week say" and "did it reach him" are two questions
    with two answers, and a resend must never read as a second run."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _run_and_send(conn, golden_config, tmp_path, sender)
        emails = _rows(
            conn,
            "SELECT report_id, recipients, subject, status FROM report_emails "
            "WHERE entity_id = 'ithrive'",
        )
        tasks = _rows(
            conn,
            f"SELECT count(*) FROM tasks WHERE entity_id = 'ithrive' "
            f"AND kind = '{SHANNON_REPLENISHMENT}'",
        )
        reports = _rows(conn, "SELECT count(*) FROM reports WHERE entity_id = 'ithrive'")
    assert len(emails) == 1
    assert str(emails[0][1]) == "zach@ithrivemedical.com"
    assert str(emails[0][3]) == "SENT"
    assert tasks[0][0] == 1, "sending did not create a run"
    assert reports[0][0] == 1, "sending did not create a report"


# --- durability -----------------------------------------------------------


def test_the_finished_file_is_the_only_one_left_behind(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Written to a temporary name and renamed into place, so a half-written
    report never appears under a name Zach might open."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        summary = _run_and_send(conn, golden_config, tmp_path, sender)
    assert summary.report_path is not None
    files = sorted(path.name for path in tmp_path.iterdir())
    assert files == [Path(summary.report_path).name]
    assert not [name for name in files if ".writing-" in name]


def test_a_crash_while_writing_leaves_neither_a_file_nor_a_row(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database work is one transaction and the rename happens after it.

    A failure part-way therefore leaves the folder as it was, not a file
    with no row behind it — which was the Phase 1 hole.
    """
    import agent_org.broker.executors.internal_report as module

    def explode(self: object, payload: dict[str, object], report_id: str) -> None:
        raise RuntimeError("the machine lost power here")

    monkeypatch.setattr(module.ReportWriter, "_record_manual_proposals", explode)
    # The failure is allowed out of the session, which is what rolls the
    # transaction back — exactly as a crashing process would.
    with (
        pytest.raises(RuntimeError, match="lost power"),
        entity_session(app_conn, entity_id) as conn,
    ):
        run_replenishment(
            conn=conn,
            config=golden_config,
            fixtures=DATA,
            output_dir=tmp_path,
            now=MONDAY,
        )
    with entity_session(app_conn, entity_id) as conn:
        reports = _rows(conn, "SELECT count(*) FROM reports WHERE entity_id = 'ithrive'")
    assert sorted(path.name for path in tmp_path.iterdir()) == []
    assert reports[0][0] == 0


def test_a_regenerated_week_keeps_the_report_it_replaced(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Superseded, never deleted: the old row points at the file it now
    lives in, and the folder and the table agree about which is current."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _run_and_send(conn, golden_config, tmp_path, sender)
        _run_and_send(conn, golden_config, tmp_path, sender, again=True)
        rows = _rows(
            conn,
            "SELECT file_path, superseded_by IS NOT NULL FROM reports "
            "WHERE entity_id = 'ithrive' ORDER BY created_at",
        )
    assert len(rows) == 2
    assert rows[0][1] is True and rows[1][1] is False
    assert ".superseded-" in str(rows[0][0])
    assert Path(str(rows[0][0])).exists(), "the replaced report is still readable"
    assert len(sender.sent) == 2, "the regenerated report is the one worth reading, so it is sent"


# --- a failed week, and a finished one ------------------------------------


def test_a_week_that_failed_runs_again_without_a_flag(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Nothing was completed, so there is nothing to supersede and no
    reason to make Zach type a flag on a Monday morning."""
    with entity_session(app_conn, entity_id) as conn:
        failed = run_replenishment(
            conn=conn,
            config=golden_config,
            fixtures=tmp_path / "not-a-fixture-directory",
            output_dir=tmp_path,
            now=MONDAY,
        )
        assert failed.error is not None
        assert failed.outcome is None

        recovered = run_replenishment(
            conn=conn,
            config=golden_config,
            fixtures=DATA,
            output_dir=tmp_path,
            now=MONDAY,
        )
    assert recovered.error is None
    assert recovered.report_path is not None


def test_a_week_that_finished_needs_the_flag(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The guard keys on completion, not on attempt."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _run_and_send(conn, golden_config, tmp_path, sender)
        with pytest.raises(RunAlreadyDone) as raised:
            run_replenishment(
                conn=conn,
                config=golden_config,
                fixtures=DATA,
                output_dir=tmp_path,
                now=MONDAY,
            )
        assert "--again" in str(raised.value)
        regenerated = run_replenishment(
            conn=conn,
            config=golden_config,
            fixtures=DATA,
            output_dir=tmp_path,
            now=MONDAY,
            again=True,
        )
    assert regenerated.error is None


def test_next_week_is_simply_next_week(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The guard is per week, not a lock on running at all."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        first = _run_and_send(conn, golden_config, tmp_path, sender)
        second = _run_and_send(conn, golden_config, tmp_path, sender, when=NEXT_MONDAY)
    assert first.task.schedule_slot != second.task.schedule_slot
    assert second.error is None
    assert len(sender.sent) == 2
