"""A real Postgres, a stub SMTP server and the app as a real process.

The posts claim what happens when a process stops at a given moment, so the tests run each step
with uvicorn in a subprocess and kill it there. Postgres runs in Podman.
"""

import asyncio
import imaplib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from email import message_from_bytes
from pathlib import Path

import asyncpg
import httpx
import pytest
from aiosmtpd.controller import Controller

DEMO = Path(__file__).parent.parent
STEPS = DEMO / "steps"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(check: Callable[[], bool], timeout: float = 20, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out after {timeout} s waiting for {what}")


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    port = free_port()
    name = f"lost-doubled-unseen-{port}"
    subprocess.run(
        ["podman", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:5432"]
        + ["-e", "POSTGRES_PASSWORD=secret", "docker.io/library/postgres:17-alpine"],
        check=True,
        capture_output=True,
    )
    url = f"postgresql://postgres:secret@127.0.0.1:{port}/postgres"
    try:
        wait_until(lambda: _can_connect(url), timeout=60, what="Postgres")
        yield url
    finally:
        subprocess.run(["podman", "rm", "-f", name], capture_output=True)


def _can_connect(url: str) -> bool:
    async def connect() -> None:
        conn = await asyncpg.connect(url)
        await conn.close()

    try:
        asyncio.run(connect())
    except OSError, asyncpg.PostgresError:
        return False
    return True


class Database:
    def __init__(self, url: str) -> None:
        self.url = url

    def run(self, sql: str, *args: object) -> None:
        async def go() -> None:
            conn = await asyncpg.connect(self.url)
            try:
                await conn.execute(sql, *args)
            finally:
                await conn.close()

        asyncio.run(go())

    def rows(self, sql: str, *args: object) -> list[dict]:
        async def go() -> list[dict]:
            conn = await asyncpg.connect(self.url)
            try:
                return [dict(r) for r in await conn.fetch(sql, *args)]
            finally:
                await conn.close()

        return asyncio.run(go())


@pytest.fixture
def db(postgres_url: str) -> Database:
    database = Database(postgres_url)
    database.run(
        "DROP TABLE IF EXISTS family, plans, meals, slots, emails, grelmicro_outbox CASCADE"
    )
    database.run((DEMO / "schema.sql").read_text())
    return database


class Smtp:
    """An SMTP server that can be slow, down, or call back on each message."""

    def __init__(self) -> None:
        self.port = free_port()
        self.messages: list = []
        self.delay = 0.0
        self.on_message: Callable[[], None] | None = None
        self.started = threading.Event()  # a send reached the server
        self.reject: set[str] = set()  # addresses refused, as a full mailbox would be
        self.refused: list[str] = []  # the addresses actually refused
        self.hold: threading.Event | None = None  # keeps a taken mail until set
        self.senders: list[str] = []  # the envelope sender of each mail taken
        self.lose_answers = 0  # take the mail, then drop the connection
        self._lock = threading.Lock()
        self._controller: Controller | None = None

    async def handle_MAIL(self, server, session, envelope, address, mail_options):  # noqa: N802
        self.started.set()
        await asyncio.sleep(self.delay)  # a slow SMTP server, before it takes the mail
        envelope.mail_from = address
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
        if address in self.reject:
            self.refused.append(address)
            return "550 No such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        with self._lock:
            self.messages.append(message_from_bytes(envelope.content))
            self.senders.append(envelope.mail_from)
            lose = self.lose_answers > 0
            self.lose_answers -= lose
        if lose:
            server.transport.close()  # taken, but the client never hears it
            return "250 Message accepted for delivery"
        while self.hold and not self.hold.is_set():
            await asyncio.sleep(0.05)  # cancellable, so a failed test cannot hang
        if self.on_message:
            self.on_message()
        return "250 Message accepted for delivery"

    def up(self) -> None:
        self._controller = Controller(self, hostname="127.0.0.1", port=self.port)
        self._controller.start()

    def down(self) -> None:
        if self._controller:
            self._controller.stop()
            self._controller = None

    def sent_to(self, address: str) -> int:
        with self._lock:
            return sum(m["To"] == address for m in self.messages)


@pytest.fixture
def smtp() -> Iterator[Smtp]:
    server = Smtp()
    server.up()
    try:
        yield server
    finally:
        server.down()


@pytest.fixture(scope="session")
def imap_port() -> Iterator[int]:
    """A real mail server with IMAP, for the bounce mailbox. Runs in Podman."""
    port = free_port()
    name = f"lost-doubled-unseen-imap-{port}"
    opts = "-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled"
    subprocess.run(
        ["podman", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:3143"]
        + ["-e", f"GREENMAIL_OPTS={opts}", "docker.io/greenmail/standalone:2.1.3"],
        check=True,
        capture_output=True,
    )
    try:
        wait_until(lambda: _can_login(port), timeout=60, what="IMAP")
        yield port
    finally:
        subprocess.run(["podman", "rm", "-f", name], capture_output=True)


def _can_login(port: int) -> bool:
    try:
        imaplib.IMAP4("127.0.0.1", port).login("probe@example.com", "x")
    except OSError, imaplib.IMAP4.error:
        return False
    return True


class Mailbox:
    """The bounce mailbox, as the app reads it. A fresh user per test."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.user = f"bounces-{free_port()}@planner.example.com"

    def _open(self) -> imaplib.IMAP4:
        imap = imaplib.IMAP4("127.0.0.1", self.port)
        imap.login(self.user, "x")
        return imap

    def deliver(self, raw: bytes) -> None:
        imap = self._open()
        try:
            imap.append("INBOX", None, None, raw)
        finally:
            imap.logout()

    def count(self) -> int:
        imap = self._open()
        try:
            imap.select("INBOX")
            _, data = imap.search(None, "ALL")
            return len(data[0].split())
        finally:
            imap.logout()


@pytest.fixture
def mailbox(imap_port: int) -> Mailbox:
    return Mailbox(imap_port)


def app_settings(postgres_url: str, smtp: Smtp, mailbox: Mailbox) -> dict[str, str]:
    """What every step reads from the environment, with short outbox timings."""
    return {
        "POSTGRES_URL": postgres_url,
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(smtp.port),
        "IMAP_HOST": "127.0.0.1",
        "IMAP_PORT": str(mailbox.port),
        "IMAP_USER": mailbox.user,
        "GREL_OPS_PORT": str(free_port()),  # each app its own ops port
        # Short timings, so a crash test does not wait 30 s for a lease.
        "GREL_OUTBOX_POLL_INTERVAL": "0.2",
        "GREL_OUTBOX_LEASE_DURATION": "2",
        "GREL_OUTBOX_RETRY_BASE": "0.2",
        "GREL_OUTBOX_RETRY_MAX": "0.5",
        "GREL_OUTBOX_RETRY_JITTER": "0",
    }


class App:
    """One step of the demo, run by uvicorn in its own process."""

    def __init__(self, step: str, env: dict[str, str]) -> None:
        self.step = step
        self.env = env
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    def start(self) -> App:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{self.step}:app", "--port", str(self.port)]
            + ["--log-level", "warning"],
            cwd=STEPS,
            env={**os.environ, **self.env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_until(self._ready, what=f"{self.step} to start")
        return self

    def _ready(self) -> bool:
        try:
            return httpx.get(f"{self.url}/openapi.json", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def kill(self) -> None:
        """Stop the process at once, as a crash or a host reboot would."""
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait()

    def stop(self, timeout: float = 30) -> None:
        """Stop the process the way a deploy does: SIGTERM, then SIGKILL after the timeout."""
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill()


@pytest.fixture
def run(
    postgres_url: str, smtp: Smtp, mailbox: Mailbox, db: Database
) -> Iterator[Callable[..., App]]:
    apps: list[App] = []

    def start(step: str, **env: str) -> App:
        app = App(step, {**app_settings(postgres_url, smtp, mailbox), **env})
        apps.append(app)  # before start, so a failed start is cleaned up too
        return app.start()

    try:
        yield start
    finally:
        for app in apps:
            app.kill()
