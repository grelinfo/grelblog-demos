"""Part 3: a rejected email. Each test name ends with its rule id in SPEC.md."""

import asyncio
import importlib
import sys
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from conftest import STEPS, Database, Mailbox, Smtp, app_settings, wait_until

ALICE = "alice@example.com"
BOB = "bob@example.com"
PASTA = {"recipe": "pasta"}
MEALS = "/plans/2026-W45/meals"


@pytest.fixture(autouse=True)
def family(db: Database) -> None:
    db.run("INSERT INTO family (email) VALUES ($1), ($2)", ALICE, BOB)


def email(db: Database, meal_id: uuid.UUID) -> dict:
    """The email record of a meal, {} before the relay picked it up."""
    rows = db.rows("SELECT message_id, status FROM emails WHERE meal_id = $1", meal_id)
    return rows[0] if rows else {}


def accepted(db: Database, meal_id: uuid.UUID) -> bool:
    return email(db, meal_id).get("status") == "accepted"


def report(message_id: object, action: str, user: str = "bounces") -> bytes:
    """A delivery status notification (RFC 3464), as a mail server sends it back."""
    return (
        f"From: MAILER-DAEMON@mx.example.com\r\n"
        f"To: {user}+{message_id}@planner.example.com\r\n"
        f"Subject: Delivery Status Notification\r\n"
        f"MIME-Version: 1.0\r\n"
        f'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
        f"\r\n--b\r\nContent-Type: text/plain\r\n\r\nDelivery to bob@example.com {action}.\r\n"
        f"--b\r\nContent-Type: message/delivery-status\r\n\r\n"
        f"Reporting-MTA: dns; mx.example.com\r\n\r\n"
        f"Final-Recipient: rfc822; bob@example.com\r\nAction: {action}\r\nStatus: 5.2.2\r\n"
        f"\r\n--b--\r\n"
    ).encode()


def out_of_office(message_id: object) -> bytes:
    return (
        f"From: bob@example.com\r\nTo: bounces+{message_id}@planner.example.com\r\n"
        f"Subject: Out of office\r\nAuto-Submitted: auto-replied\r\n\r\nBack on Monday.\r\n"
    ).encode()


@pytest.fixture
def in_process(
    postgres_url: str, smtp: Smtp, mailbox: Mailbox, db: Database, monkeypatch
) -> Iterator[tuple[object, TestClient, str]]:
    """Step 5 in this process, to call its task directly. Yields the module, a client, /metrics."""
    settings = app_settings(postgres_url, smtp, mailbox)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GREL_OUTBOX_MAX_ATTEMPTS", "2")
    monkeypatch.syspath_prepend(str(STEPS))
    sys.modules.pop("5_delivery", None)
    step = importlib.import_module("5_delivery")
    with TestClient(step.app) as client:
        yield step, client, f"http://127.0.0.1:{settings['GREL_OPS_PORT']}/metrics"


def metric(metrics_url: str, name: str) -> float:
    """The sum of a Prometheus metric over its labels, 0 if absent."""
    total = 0.0
    for line in httpx.get(metrics_url).text.splitlines():
        if line.startswith(name + "{") or line.startswith(name + " "):
            total += float(line.rsplit(" ", 1)[1])
    return total


def test_retried_add_queues_one_email_P3_R01(run, smtp: Smtp, db: Database) -> None:
    app = run("5_delivery")
    meal_id = uuid.uuid4()
    url = f"{app.url}{MEALS}/{meal_id}"
    with ThreadPoolExecutor(max_workers=5) as pool:
        answers = list(pool.map(lambda _: httpx.put(url, json=PASTA), range(5)))
    assert {a.status_code for a in answers} == {200}
    wait_until(lambda: accepted(db, meal_id), what="the send")
    # A late retry, after delivery, still queues nothing: the row is kept a week.
    assert httpx.put(url, json=PASTA).status_code == 200
    time.sleep(1.5)
    assert len(smtp.messages) == 1
    assert smtp.messages[0]["To"] == f"{ALICE}, {BOB}"
    assert db.rows("SELECT dedup_key FROM grelmicro_outbox") == [{"dedup_key": str(meal_id)}]


class Ping(BaseModel):
    n: int


def test_dedup_window_ends_at_delivery_P3_R02(postgres_url: str, db: Database, monkeypatch) -> None:
    from grelmicro import Grelmicro
    from grelmicro.outbox import Outbox
    from grelmicro.providers.postgres import PostgresProvider

    monkeypatch.setenv("POSTGRES_URL", postgres_url)
    monkeypatch.setenv("GREL_OUTBOX_POLL_INTERVAL", "0.2")

    async def publish_twice(keep: timedelta) -> tuple[bool, bool]:
        """Publish, wait until the message is delivered and settled, publish again."""
        postgres = PostgresProvider()
        outbox = Outbox(postgres, keep_delivered=keep)
        handled = asyncio.Event()

        @outbox.handler(Ping)
        async def handle(message) -> None:
            handled.set()

        async def publish() -> bool:
            async with postgres.client.acquire() as conn, conn.transaction():
                return await outbox.publish(conn, Ping(n=1), dedup_key="meal-1")

        async def settled() -> bool:
            conn = await asyncpg.connect(postgres_url)
            try:
                return not await conn.fetchval(
                    "SELECT count(*) FROM grelmicro_outbox WHERE state <> 'delivered'"
                )
            finally:
                await conn.close()

        async with Grelmicro(uses=[postgres, outbox]):
            first = await publish()
            await asyncio.wait_for(handled.wait(), 10)
            async with asyncio.timeout(10):
                while not await settled():
                    await asyncio.sleep(0.1)
            return first, await publish()

    # The default deletes the row on delivery: the same key is new again.
    assert asyncio.run(publish_twice(timedelta(0))) == (True, True)
    db.run("DROP TABLE grelmicro_outbox")
    # With a window, the delivered row keeps the key.
    assert asyncio.run(publish_twice(timedelta(days=7))) == (True, False)


def test_return_address_per_email_P3_R03(run, smtp: Smtp, db: Database) -> None:
    app = run("5_delivery")
    meal_id = uuid.uuid4()
    assert httpx.put(f"{app.url}{MEALS}/{meal_id}", json=PASTA).status_code == 200
    wait_until(lambda: accepted(db, meal_id), what="the send")
    message_id = email(db, meal_id)["message_id"]
    assert smtp.senders == [f"bounces+{message_id}@planner.example.com"]
    assert smtp.messages[0]["Message-ID"] == f"<{message_id}@planner.example.com>"


def test_bounce_marks_the_email_once_P3_R04(
    in_process, smtp: Smtp, mailbox: Mailbox, db: Database
) -> None:
    step, client, _ = in_process
    meal_id = uuid.uuid4()
    client.put(f"{MEALS}/{meal_id}", json=PASTA)
    wait_until(lambda: accepted(db, meal_id), what="the send")
    message_id = email(db, meal_id)["message_id"]
    mailbox.deliver(out_of_office(message_id))
    mailbox.deliver(report(message_id, "delayed"))
    client.portal.call(step.read_bounces)
    assert email(db, meal_id)["status"] == "accepted"  # neither is a bounce
    assert mailbox.count() == 0  # read, then deleted
    mailbox.deliver(report(message_id, "failed"))
    mailbox.deliver(report(message_id, "failed"))  # the same bounce, read twice
    client.portal.call(step.read_bounces)
    assert email(db, meal_id)["status"] == "bounced"
    assert mailbox.count() == 0


def test_no_bounce_is_not_delivered_P3_R05(in_process, smtp: Smtp, db: Database) -> None:
    step, client, _ = in_process
    meal_id = uuid.uuid4()
    client.put(f"{MEALS}/{meal_id}", json=PASTA)
    wait_until(lambda: accepted(db, meal_id), what="the send")
    client.portal.call(step.read_bounces)
    statuses = {r["status"] for r in db.rows("SELECT status FROM emails")}
    assert statuses == {"accepted"}  # there is no "delivered"


def test_lost_answer_sends_twice_P3_R06(run, smtp: Smtp, db: Database) -> None:
    smtp.lose_answers = 1  # the server takes the mail, its answer never arrives
    app = run("5_delivery")
    meal_id = uuid.uuid4()
    httpx.put(f"{app.url}{MEALS}/{meal_id}", json=PASTA)
    wait_until(lambda: accepted(db, meal_id), what="the send")
    assert len(smtp.messages) == 2  # SMTP has no key to stop the second copy
    assert smtp.messages[0]["Message-ID"] == smtp.messages[1]["Message-ID"]


def test_failures_are_metrics_P3_R07(
    in_process, smtp: Smtp, mailbox: Mailbox, db: Database
) -> None:
    step, client, metrics_url = in_process
    sent = uuid.uuid4()
    client.put(f"{MEALS}/{sent}", json=PASTA)
    wait_until(lambda: accepted(db, sent), what="the send")
    smtp.down()
    client.put(f"{MEALS}/{uuid.uuid4()}", json=PASTA)
    wait_until(
        lambda: metric(metrics_url, "grelmicro_outbox_dead_lettered_total") == 1,
        what="the dead letter",
    )
    # A growing backlog shows as published faster than delivered plus dead.
    assert metric(metrics_url, "grelmicro_outbox_published_total") == 2
    assert metric(metrics_url, "grelmicro_outbox_delivered_total") == 1
    message_id = email(db, sent)["message_id"]
    mailbox.deliver(report(message_id, "failed"))
    mailbox.deliver(report(message_id, "failed"))
    client.portal.call(step.read_bounces)
    assert metric(metrics_url, "emails_bounced_total") == 1  # counted once
