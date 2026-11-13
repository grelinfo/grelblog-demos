"""SMTP helpers shared by every step. Not shown in the posts."""

import asyncio
import imaplib
import os
import re
from email import message_from_bytes
from email.message import EmailMessage, Message
from uuid import UUID

import aiosmtplib
from pydantic import BaseModel


class Email(BaseModel):
    """What a template gives: the subject and the body."""

    subject: str
    body: str


TEMPLATES = {
    "plan_ready": Email(
        subject="The meal plan is ready",
        body="The plan for week {week} is ready: "
        "https://planner.example.com/plans/{week}",
    ),
    "meal_added": Email(
        subject="A meal was added to the plan",
        body="{recipe} is in the plan for week {week}.",
    ),
}


def render(template: str, **params: str) -> Email:
    email = TEMPLATES[template]
    return Email(
        subject=email.subject, body=email.body.format(**params)
    )


async def send_email(
    to: str | list[str],
    email: Email,
    *,
    sender: str | None = None,
    message_id: str | None = None,
) -> None:
    """Send one email. Bounces go to `sender`."""
    message = EmailMessage()
    message["From"] = "planner@example.com"
    message["To"] = to if isinstance(to, str) else ", ".join(to)
    if message_id:
        message["Message-ID"] = message_id
    message["Subject"] = email.subject
    message.set_content(email.body)
    # The whole send stays below the relay lease (30 s).
    async with asyncio.timeout(10):
        await aiosmtplib.send(
            message,
            sender=sender,
            hostname=os.environ.get("SMTP_HOST", "localhost"),
            port=int(os.environ.get("SMTP_PORT", "1025")),
        )


class Bounce(BaseModel):
    """A message read from the bounce mailbox."""

    uid: str
    message_id: UUID | None  # from the address it came back to
    failed: bool  # a delivery report that says the delivery failed


VERP = re.compile(r"bounces\+([0-9a-f-]{36})@")


def _failed(report: Message) -> bool:
    """True for a delivery report (RFC 3464) of a failure."""
    if report.get_content_type() != "multipart/report":
        return False  # an out-of-office reply, for example
    for part in report.walk():
        if part.get_content_type() == "message/delivery-status":
            blocks = part.get_payload()
            assert isinstance(blocks, list)
            return any(
                isinstance(b, Message)
                and str(b.get("Action", "")).strip().lower()
                == "failed"
                for b in blocks
            )
    return False


def _imap() -> imaplib.IMAP4:
    imap = imaplib.IMAP4(
        os.environ.get("IMAP_HOST", "localhost"),
        int(os.environ.get("IMAP_PORT", "143")),
    )
    imap.login(
        os.environ.get("IMAP_USER", "bounces@planner.example.com"),
        os.environ.get("IMAP_PASSWORD", ""),
    )
    imap.select("INBOX")
    return imap


def fetch_bounces() -> list[Bounce]:
    """Read the bounce mailbox. Blocking: run in a thread."""
    imap = _imap()
    try:
        _, data = imap.uid("SEARCH", "ALL")
        bounces = []
        for uid in data[0].decode().split():
            _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = parts[0]
            assert isinstance(raw, tuple)
            report = message_from_bytes(raw[1])
            match = VERP.search(str(report.get("To", "")))
            bounces.append(
                Bounce(
                    uid=uid,
                    message_id=UUID(match[1]) if match else None,
                    failed=_failed(report),
                )
            )
        return bounces
    finally:
        imap.logout()


def delete_read(bounces: list[Bounce]) -> None:
    """Delete the messages read. Blocking: run it in a thread."""
    if not bounces:
        return
    imap = _imap()
    try:
        uids = ",".join(b.uid for b in bounces)
        imap.uid("STORE", uids, "+FLAGS", r"(\Deleted)")
        imap.expunge()
    finally:
        imap.logout()
