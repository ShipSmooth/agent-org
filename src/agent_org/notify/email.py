"""Sending the weekly report by email — the one thing Shannon sends.

There is no address in this file. The recipients arrive already resolved
from `config/<entity>/shannon.yaml`, where role names map to real
addresses per business, so adding a recipient or a second LLC is a config
edit rather than a search through source. The vendor identity
(shipsmooth.com) is refused at config load, not here.

Credentials come from the environment, prefixed per entity, e.g.
ITHRIVE_SMTP_PASSWORD. A literal in source fails CI, and no real one is
ever handled by whoever wrote this.

Failure is loud and non-fatal. The report is already on disk and in the
database before this is called; a bad minute at the mail server must not
be able to erase a week's work, and must not be able to hide it either.
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol

SMTP_HOST_VAR = "SMTP_HOST"
SMTP_PORT_VAR = "SMTP_PORT"
SMTP_USERNAME_VAR = "SMTP_USERNAME"
SMTP_PASSWORD_VAR = "SMTP_PASSWORD"
DEFAULT_PORT = 587


class SendFailed(Exception):
    """The mail server would not take the report.

    Never swallowed and never fatal: the caller records the attempt, says
    so at the top of its output, and leaves the written report alone.
    """


@dataclass(frozen=True)
class Mail:
    from_name: str
    from_address: str
    to: tuple[str, ...]
    subject: str
    body: str

    def as_message(self) -> EmailMessage:
        message = EmailMessage()
        message["From"] = f"{self.from_name} <{self.from_address}>"
        message["To"] = ", ".join(self.to)
        message["Subject"] = self.subject
        # Reports are plain text on purpose: Zach reads them on a phone,
        # and the file on disk and the email say the same thing byte for
        # byte, so a question about one is answerable from the other.
        message.set_content(self.body)
        return message


class Sender(Protocol):
    """Anything that can deliver a message. SMTP in production."""

    def send(self, mail: Mail) -> str: ...


@dataclass(frozen=True)
class SmtpSender:
    """The real thing: STARTTLS, then authenticate, then send.

    `credentials_prefix` is the entity's prefix, so two businesses can send
    from two mailboxes without either one's password being reachable by the
    other's configuration.
    """

    credentials_prefix: str = ""
    timeout_seconds: float = 30.0

    def _setting(self, name: str) -> str:
        return os.environ.get(f"{self.credentials_prefix}{name}", "").strip()

    def send(self, mail: Mail) -> str:
        host = self._setting(SMTP_HOST_VAR)
        username = self._setting(SMTP_USERNAME_VAR)
        password = self._setting(SMTP_PASSWORD_VAR)
        missing = [
            f"{self.credentials_prefix}{name}"
            for name, value in (
                (SMTP_HOST_VAR, host),
                (SMTP_USERNAME_VAR, username),
                (SMTP_PASSWORD_VAR, password),
            )
            if not value
        ]
        if missing:
            raise SendFailed(
                "The report was written but not emailed: "
                + ", ".join(missing)
                + " is not set in the environment. Nothing is lost — the report is on "
                "disk and in the database — but nobody has been told about it."
            )
        port_text = self._setting(SMTP_PORT_VAR)
        try:
            port = int(port_text) if port_text else DEFAULT_PORT
        except ValueError as exc:
            raise SendFailed(
                f"{self.credentials_prefix}{SMTP_PORT_VAR} is '{port_text}', which is "
                "not a port number. The report was written; it was not sent."
            ) from exc
        try:
            with smtplib.SMTP(host, port, timeout=self.timeout_seconds) as smtp:
                smtp.starttls()
                smtp.login(username, password)
                smtp.send_message(mail.as_message())
        except (OSError, smtplib.SMTPException) as exc:
            raise SendFailed(
                f"The mail server at {host}:{port} would not take this week's report "
                f"({exc.__class__.__name__}: {exc}). The report is written and safe; "
                "it has not reached anyone."
            ) from exc
        return f"{host}:{port}"


@dataclass
class RecordingSender:
    """A sender that keeps the message instead of delivering it.

    Used by the tests and by a dry run: the subject line, the recipients
    and the body are all checkable without a mail server and without a
    credential.
    """

    sent: list[Mail] = field(default_factory=list)
    fail_with: str | None = None

    def send(self, mail: Mail) -> str:
        if self.fail_with is not None:
            raise SendFailed(self.fail_with)
        self.sent.append(mail)
        return "recorded, not sent"


def subject_line(week: str, lines_needing_an_order: int, blocked: int) -> str:
    """What Zach sees in a notification list, without opening anything.

    The week, the count, and whether anything is stuck. He reads this on a
    phone with the laptop shut, so the headline cannot be inside the body.
    """
    if lines_needing_an_order == 0:
        headline = "nothing to order"
    elif lines_needing_an_order == 1:
        headline = "1 line to order"
    else:
        headline = f"{lines_needing_an_order} lines to order"
    if blocked == 1:
        headline += ", 1 line blocked"
    elif blocked > 1:
        headline += f", {blocked} lines blocked"
    return f"Shannon — week of {week} — {headline}"


__all__ = [
    "DEFAULT_PORT",
    "Mail",
    "RecordingSender",
    "SendFailed",
    "Sender",
    "SmtpSender",
    "subject_line",
]
