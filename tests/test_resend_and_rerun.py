"""Running a week again, and posting a report that was already written.

Two commands that look similar and are not. `shannon run --again` works
the week out afresh and supersedes its report; `shannon resend` reads
nothing, calculates nothing and puts the report that already exists in the
post a second time.

The bug these tests pin: a week Zach re-ran on purpose was spending the
crash-retry budget, so the third `--again` of one week was refused — and
refused with a sentence claiming the report "has been emailed" when its
only delivery attempt had been a 535 from the mail server.

No mail server is contacted here and no credential exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from agent_org.config.models import LoadedConfig
from agent_org.db.connection import entity_session
from agent_org.notify.email import RecordingSender
from agent_org.runtime.worker import (
    NothingToResend,
    RunAlreadyDone,
    deliver_report,
    resend_report,
    run_replenishment,
)

DATA = Path(__file__).parent / "fixtures" / "golden" / "data"
MONDAY = datetime(2026, 3, 30, 6, 0, tzinfo=UTC)
WEEK = "2026-W14"


def _run(
    conn: psycopg.Connection[tuple[object, ...]],
    config: LoadedConfig,
    output_dir: Path,
    sender: RecordingSender,
    again: bool = False,
) -> object:
    summary = run_replenishment(
        conn=conn,
        config=config,
        fixtures=DATA,
        output_dir=output_dir,
        now=MONDAY,
        again=again,
    )
    return deliver_report(conn=conn, config=config, summary=summary, sender=sender)


def _emails(conn: psycopg.Connection[tuple[object, ...]]) -> list[tuple[object, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, error, subject FROM report_emails ORDER BY attempted_at, status"
        )
        return list(cur.fetchall())


def test_a_week_can_be_run_again_as_often_as_zach_asks(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """`attempts` and `max_attempts` are a budget for crashes: two retries
    and stop. A deliberate re-run is not a crash, and used to be paid for
    out of the same purse, so the third `--again` of a week was told the
    week had already been carried out."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, sender)
        for _ in range(4):
            summary = run_replenishment(
                conn=conn,
                config=golden_config,
                fixtures=DATA,
                output_dir=tmp_path,
                now=MONDAY,
                again=True,
            )
            assert summary.error is None, summary.error
            assert summary.superseded_report_id is not None
        with conn.cursor() as cur:
            cur.execute("SELECT attempts, max_attempts FROM tasks")
            attempts, ceiling = next(iter(cur.fetchall()))
    # The counters keep counting — the attempt number is what tells one
    # re-run's report from the last — and the ceiling rises to meet them.
    assert attempts == 5
    assert ceiling == 5


def test_the_guard_does_not_claim_a_failed_send_arrived(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """The sentence Zach was shown asserted delivery without ever reading
    `report_emails`. The week was complete; the email was a 535."""
    sender = RecordingSender(fail_with="(535, 'Username and Password not accepted')")
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, sender)
        with pytest.raises(RunAlreadyDone) as raised:
            run_replenishment(
                conn=conn,
                config=golden_config,
                fixtures=DATA,
                output_dir=tmp_path,
                now=MONDAY,
            )
    message = str(raised.value)
    assert "has NOT been emailed" in message
    assert "535" in message
    assert "shannon resend" in message


def test_the_guard_says_who_it_reached_when_it_did(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Delivered is also a fact worth reading off the table rather than
    assuming — and it names the address it reached."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, sender)
        with pytest.raises(RunAlreadyDone) as raised:
            run_replenishment(
                conn=conn,
                config=golden_config,
                fixtures=DATA,
                output_dir=tmp_path,
                now=MONDAY,
            )
    message = str(raised.value)
    assert "was emailed to" in message
    assert "NOT been emailed" not in message
    for address in golden_config.shannon.report_email_addresses():
        assert address in message


def test_resend_posts_the_same_report_without_working_the_week_out_again(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Zach's case: the numbers were right, the mail server was not. The
    report is not regenerated — the bytes that were filed are the bytes
    that are sent."""
    failing = RecordingSender(fail_with="(535, 'Username and Password not accepted')")
    working = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        first = _run(conn, golden_config, tmp_path, failing)
        assert getattr(first, "email_error", None) is not None
        with conn.cursor() as cur:
            cur.execute("SELECT id, body FROM reports")
            reports_before = list(cur.fetchall())
        summary = resend_report(
            conn=conn,
            config=golden_config,
            week=WEEK,
            now=MONDAY,
            sender=working,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT id, body FROM reports")
            reports_after = list(cur.fetchall())
        attempts = _emails(conn)

    assert summary.error is None, summary.error
    assert len(working.sent) == 1
    # No second report, and the same body: a resend is a delivery, not a run.
    assert reports_after == reports_before
    assert working.sent[0].body == str(reports_before[0][1])
    assert working.sent[0].subject == summary.subject
    # Both attempts are kept, in order, so "did it ever arrive" has an answer.
    assert [str(row[0]) for row in attempts] == ["FAILED", "SENT"]


def test_resend_of_a_delivered_report_is_a_second_delivery_not_a_duplicate(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """Asked for on purpose, so it happens. The broker recognises a repeat
    of an action by its fingerprint, and a resend carries its own, or it
    would hand back the earlier attempt without troubling the mail server."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, sender)
        summary = resend_report(
            conn=conn, config=golden_config, week=WEEK, now=MONDAY, sender=sender
        )
        attempts = _emails(conn)
    assert summary.error is None, summary.error
    assert len(sender.sent) == 2
    assert [str(row[0]) for row in attempts] == ["SENT", "SENT"]


def test_a_failed_resend_keeps_the_report_and_records_the_attempt(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """A second bad minute at the mail server is still not a lost week."""
    failing = RecordingSender(fail_with="the mail server refused the connection")
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, failing)
        summary = resend_report(
            conn=conn, config=golden_config, week=WEEK, now=MONDAY, sender=failing
        )
        attempts = _emails(conn)
    assert summary.error is not None
    assert "refused the connection" in summary.error
    assert Path(summary.report.file_path).exists()
    assert [str(row[0]) for row in attempts] == ["FAILED", "FAILED"]


def test_resend_refuses_a_week_that_has_no_report(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
) -> None:
    """Nothing written, nothing to post, and it says so rather than
    inventing a week."""
    with (
        entity_session(app_conn, entity_id) as conn,
        pytest.raises(NothingToResend) as raised,
    ):
        resend_report(conn=conn, config=golden_config, week="2026-W02", now=MONDAY)
    assert "2026-W02" in str(raised.value)


def test_resend_refuses_a_report_that_was_never_offered_to_the_mail_server(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """A `--no-email` run records no subject line, and the subject carries
    the headline — how many lines need an order, and what is blocked. Making
    one up here would mean counting the week again in a command whose whole
    promise is that it does not, so it says so and sends the operator to
    `--again` instead."""
    with entity_session(app_conn, entity_id) as conn:
        run_replenishment(
            conn=conn,
            config=golden_config,
            fixtures=DATA,
            output_dir=tmp_path,
            now=MONDAY,
        )
        with pytest.raises(NothingToResend) as raised:
            resend_report(conn=conn, config=golden_config, week=WEEK, now=MONDAY)
    assert "--no-email" in str(raised.value)


def test_resend_sends_the_report_that_stands_not_the_one_it_replaced(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    golden_config: LoadedConfig,
    tmp_path: Path,
) -> None:
    """A re-run leaves a superseded report behind. Sending that one would
    be sending numbers Shannon has already withdrawn."""
    sender = RecordingSender()
    with entity_session(app_conn, entity_id) as conn:
        _run(conn, golden_config, tmp_path, sender)
        _run(conn, golden_config, tmp_path, sender, again=True)
        summary = resend_report(
            conn=conn, config=golden_config, week=WEEK, now=MONDAY, sender=sender
        )
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM reports WHERE superseded_by IS NULL")
            live = [str(row[0]) for row in cur.fetchall()]
    assert summary.error is None, summary.error
    assert live == [summary.report.report_id]
