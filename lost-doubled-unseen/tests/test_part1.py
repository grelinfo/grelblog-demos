"""Part 1: From BackgroundTasks to an outbox. Each test name ends with its rule id in SPEC.md."""

import importlib
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import STEPS, Database, Smtp, wait_until

ALICE = "alice@example.com"
BOB = "bob@example.com"
FAMILY = (ALICE, BOB)
WEEK = "2026-W45"


@pytest.fixture(autouse=True)
def plans(db: Database) -> None:
    db.run("INSERT INTO plans (week) VALUES ('2026-W45'), ('2026-W46')")
    db.run("INSERT INTO family (email) VALUES ($1), ($2)", ALICE, BOB)


def finish(app, week: str = WEEK) -> httpx.Response:
    return httpx.post(f"{app.url}/plans/{week}/finish", timeout=30)


def finished(db: Database, week: str = WEEK) -> bool:
    return (
        db.rows("SELECT finished_at FROM plans WHERE week = $1", week)[0]["finished_at"] is not None
    )


def test_inline_send_waits_for_smtp_P1_R01(run, smtp: Smtp) -> None:
    smtp.delay = 2
    app = run("1_inline")
    start = time.monotonic()
    assert finish(app).status_code == 200
    assert time.monotonic() - start >= 4  # 2 s for Alice, then 2 s for Bob


def test_background_task_answers_before_sending_P1_R02(run, smtp: Smtp) -> None:
    smtp.delay = 2
    app = run("2_background_tasks")
    start = time.monotonic()
    assert finish(app).status_code == 200
    assert time.monotonic() - start < 1
    assert all(smtp.sent_to(p) == 0 for p in FAMILY)
    wait_until(lambda: all(smtp.sent_to(p) == 1 for p in FAMILY), what="the email")


def test_background_task_lost_on_stop_P1_R03(run, smtp: Smtp, db: Database) -> None:
    smtp.delay = 3
    app = run("2_background_tasks")
    assert finish(app).status_code == 200
    smtp.started.wait(5)
    app.kill()  # after the 200, before SMTP took the mail
    run("2_background_tasks")  # the next process knows nothing about it
    time.sleep(5)
    assert all(smtp.sent_to(p) == 0 for p in FAMILY)
    assert finished(db)  # the plan is finished, the email is gone


def test_rollback_leaves_no_email_P1_R04(
    postgres_url, smtp: Smtp, db: Database, monkeypatch
) -> None:
    monkeypatch.setenv("POSTGRES_URL", postgres_url)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")  # so a send would be seen
    monkeypatch.setenv("SMTP_PORT", str(smtp.port))
    monkeypatch.syspath_prepend(str(STEPS))
    sys.modules.pop("3_outbox", None)
    step = importlib.import_module("3_outbox")
    publish = step.outbox.publish

    async def publish_then_fail(*args, **kwargs):
        await publish(*args, **kwargs)
        raise RuntimeError("the request fails after the email was staged")

    monkeypatch.setattr(step.outbox, "publish", publish_then_fail)
    with TestClient(step.app, raise_server_exceptions=False) as client:
        assert client.post(f"/plans/{WEEK}/finish").status_code == 500
        time.sleep(1)  # give the relay time to find something
    assert not finished(db)
    assert db.rows("SELECT * FROM grelmicro_outbox") == []
    assert all(smtp.sent_to(p) == 0 for p in FAMILY)


def test_outbox_survives_stop_P1_R05(run, smtp: Smtp) -> None:
    smtp.delay = 3
    app = run("3_outbox")
    assert finish(app).status_code == 200
    smtp.started.wait(5)
    app.kill()  # the same moment as P1-R03
    smtp.delay = 0
    run("3_outbox")
    wait_until(
        lambda: all(smtp.sent_to(p) == 1 for p in FAMILY), what="the email after the restart"
    )


def test_outbox_retries_until_smtp_is_back_P1_R06(run, smtp: Smtp, db: Database) -> None:
    smtp.down()
    app = run("3_outbox")
    assert finish(app).status_code == 200
    wait_until(
        lambda: min(r["attempts"] for r in db.rows("SELECT attempts FROM grelmicro_outbox")) >= 2,
        what="two failed attempts",
    )
    smtp.up()
    wait_until(
        lambda: all(smtp.sent_to(p) == 1 for p in FAMILY), what="the email once SMTP is back"
    )


def test_dead_letter_after_max_attempts_P1_R07(run, smtp: Smtp, db: Database) -> None:
    smtp.down()
    app = run("3_outbox", GREL_OUTBOX_MAX_ATTEMPTS="3")
    assert finish(app).status_code == 200
    wait_until(
        lambda: {r["state"] for r in db.rows("SELECT state FROM grelmicro_outbox")} == {"dead"},
        what="the dead letters",
    )
    assert {r["attempts"] for r in db.rows("SELECT attempts FROM grelmicro_outbox")} == {3}
    smtp.up()
    assert finish(app, "2026-W46").status_code == 200
    wait_until(lambda: all(smtp.sent_to(p) == 1 for p in FAMILY), what="the next email")
    assert db.rows("SELECT state FROM grelmicro_outbox WHERE state = 'dead'")


def test_crash_after_send_sends_twice_P1_R08(run, smtp: Smtp) -> None:
    app = run("3_outbox", GREL_OUTBOX_CONCURRENCY="1")  # one send at a time
    smtp.hold = threading.Event()  # SMTP keeps the taken mail until the 200 is in
    smtp.on_message = app.kill  # then the relay dies before marking it done
    assert finish(app).status_code == 200
    smtp.hold.set()
    wait_until(lambda: app.process.poll() is not None, what="the process killed by the hook")
    first = [p for p in FAMILY if smtp.sent_to(p) == 1]
    assert len(first) == 1  # one email went out before the crash
    smtp.hold = None
    smtp.on_message = None
    run("3_outbox")
    wait_until(lambda: smtp.sent_to(first[0]) == 2, what="the second send of that email")
    other = next(p for p in FAMILY if p != first[0])
    wait_until(lambda: smtp.sent_to(other) == 1, what="the other email")


def test_message_stores_intent_not_body_P1_R09(run, smtp: Smtp, db: Database) -> None:
    smtp.down()
    app = run("3_outbox")
    assert finish(app).status_code == 200
    payload = str(db.rows("SELECT payload FROM grelmicro_outbox")[0]["payload"])
    assert WEEK in payload
    assert "is ready" not in payload  # the rendered sentence is not stored


def test_graceful_stop_waits_but_a_stop_timeout_loses_it_P1_R10(run, smtp: Smtp) -> None:
    smtp.delay = 3
    app = run("2_background_tasks")
    assert finish(app).status_code == 200
    smtp.started.wait(5)
    app.stop(timeout=30)  # SIGTERM: uvicorn waits for the background task
    assert app.process.returncode != -signal.SIGKILL
    assert all(smtp.sent_to(p) == 1 for p in FAMILY)

    smtp.started.clear()
    smtp.delay = 6
    app = run("2_background_tasks")
    assert finish(app, "2026-W46").status_code == 200
    smtp.started.wait(5)  # the send is in flight
    app.stop(timeout=2)  # the deploy gives up and sends SIGKILL
    assert app.process.returncode == -signal.SIGKILL
    run("2_background_tasks")
    time.sleep(6)
    assert all(smtp.sent_to(p) == 1 for p in FAMILY)  # only the first plan's emails


def test_outbox_survives_a_stop_timeout_P1_R11(run, smtp: Smtp) -> None:
    smtp.delay = 6
    app = run("3_outbox")
    assert finish(app).status_code == 200
    smtp.started.wait(5)  # the relay's send is in flight
    app.stop(timeout=2)
    assert app.process.returncode == -signal.SIGKILL
    smtp.delay = 0
    run("3_outbox")
    wait_until(lambda: all(smtp.sent_to(p) == 1 for p in FAMILY), what="the email after the deploy")


def test_delivered_message_is_deleted_P1_R12(run, smtp: Smtp, db: Database) -> None:
    app = run("3_outbox")
    assert finish(app).status_code == 200
    wait_until(lambda: all(smtp.sent_to(p) == 1 for p in FAMILY), what="the email")
    wait_until(lambda: db.rows("SELECT * FROM grelmicro_outbox") == [], what="the row deleted")


def test_one_message_per_person_P1_R13(run, smtp: Smtp, db: Database) -> None:
    smtp.reject = {BOB}  # Bob's mailbox refuses mail for now
    smtp.hold = threading.Event()  # Alice's mail waits until the rows are counted
    app = run("3_outbox")
    try:
        assert finish(app).status_code == 200
        assert len(db.rows("SELECT * FROM grelmicro_outbox")) == 2
    finally:
        smtp.hold.set()
    wait_until(lambda: smtp.sent_to(ALICE) == 1, what="Alice's email")
    bob = "SELECT attempts FROM grelmicro_outbox WHERE payload->>'to' = $1"
    wait_until(lambda: db.rows(bob, BOB)[0]["attempts"] >= 2, what="Bob's email retried")
    smtp.reject = set()
    wait_until(lambda: smtp.sent_to(BOB) == 1, what="Bob's email")
    assert smtp.sent_to(ALICE) == 1  # Alice's email was never sent again


def test_finishing_twice_sends_once_P1_R14(run, smtp: Smtp, db: Database) -> None:
    app = run("3_outbox")
    assert finish(app).status_code == 200
    assert finish(app).status_code == 200  # the answer was lost, the user retries
    with ThreadPoolExecutor(max_workers=5) as pool:  # five at once, another week
        answers = list(pool.map(lambda _: finish(app, "2026-W46"), range(5)))
    assert {a.status_code for a in answers} == {200}
    wait_until(lambda: all(smtp.sent_to(p) == 2 for p in FAMILY), what="one email per plan")
    time.sleep(2)
    assert all(smtp.sent_to(p) == 2 for p in FAMILY)


def test_background_failure_skips_the_rest_P1_R15(run, smtp: Smtp) -> None:
    smtp.reject = {ALICE}  # the first address in the list fails
    app = run("2_background_tasks")
    assert finish(app).status_code == 200
    wait_until(lambda: ALICE in smtp.refused, what="Alice's send refused")
    time.sleep(3)
    assert BOB not in smtp.refused
    assert smtp.sent_to(BOB) == 0  # Bob's task never ran
