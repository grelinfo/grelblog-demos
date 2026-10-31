import importlib.util
import shutil
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

DEMO = Path(__file__).parent.parent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


OPS_PORT = _free_port()


@pytest.fixture
def start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[Path], TestClient]]:
    """Run an app file as it is, with its database in a temporary SQLite file.

    Day two uses PostgresProvider. Here it is swapped for SQLite so the unit tests
    need no server. `pytest -m compose` runs them on a real Postgres.
    """
    from grelmicro.providers import postgres, sqlite

    original = sqlite.SQLiteProvider.__init__

    def init(self, path: str = "", *args, **kwargs) -> None:
        original(self, path.replace("/data", str(tmp_path)), *args, **kwargs)

    monkeypatch.setattr(sqlite.SQLiteProvider, "__init__", init)
    monkeypatch.setattr(postgres, "PostgresProvider", lambda: sqlite.SQLiteProvider("/data/app.db"))
    monkeypatch.setenv("GREL_OPS_PORT", str(OPS_PORT))
    shutil.copytree(DEMO / "frontend" / "build", tmp_path / "static")
    monkeypatch.chdir(tmp_path)
    clients = []

    def run(path: Path) -> TestClient:
        sys.modules.pop("main", None)
        spec = importlib.util.spec_from_file_location("main", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["main"] = module
        spec.loader.exec_module(module)
        client = TestClient(module.app)
        client.__enter__()
        clients.append(client)
        return client

    yield run
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(start: Callable[[Path], TestClient]) -> TestClient:
    """The final app, main.py (day two)."""
    return start(DEMO / "main.py")


@pytest.fixture
def ops(start: Callable[[Path], TestClient]) -> Iterator[httpx.Client]:
    """The OpsServer port: probes and /metrics."""
    with httpx.Client(base_url=f"http://127.0.0.1:{OPS_PORT}") as ops:
        yield ops
