import logging
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

DEMO = Path(__file__).parent.parent
STEPS = sorted((DEMO / "steps").glob("*.py"))
HTML = {"accept": "text/html"}


def test_main_py_is_the_last_step() -> None:
    assert (DEMO / "main.py").read_text() == STEPS[-1].read_text()


@pytest.mark.parametrize("step", STEPS, ids=lambda p: p.stem)
def test_every_step_runs(step: Path, start) -> None:
    # Log() resets the root handlers on startup, so listen on the app's own logger.
    seen: list[logging.LogRecord] = []

    class Seen(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    logging.getLogger("main").addHandler(Seen())
    client = start(step)
    assert client.get("/api/solar").json() == {"power_kw": 4.2}
    assert client.get("/about", headers=HTML).status_code == 200
    for _ in range(50):
        if any(r.getMessage().startswith("water heater") for r in seen):
            return
        time.sleep(0.1)
    pytest.fail(f"{step.stem}: the surplus task did not run")


def test_probes_answer_on_the_ops_port(client: TestClient, ops: httpx.Client) -> None:
    assert ops.get("/livez").status_code == 200
    assert ops.get("/readyz").status_code == 200
    assert client.get("/metrics", headers={"accept": "application/json"}).status_code == 404


def test_the_spa_answers_its_client_side_routes(client: TestClient) -> None:
    index = client.get("/", headers=HTML)
    assert index.status_code == 200
    assert "_app/" in index.text
    deep = client.get("/about", headers=HTML)
    assert deep.status_code == 200
    assert deep.text == index.text


def test_a_missing_api_path_is_a_real_404(client: TestClient) -> None:
    response = client.get("/api/missing", headers={"accept": "application/json"})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_the_surplus_task_runs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    # The first run can happen while the client starts, so read the setup logs too.
    for _ in range(50):
        records = caplog.get_records("setup") + caplog.records
        if any(r.getMessage().startswith("water heater") for r in records):
            return
        time.sleep(0.1)
    pytest.fail("no copy ran the surplus task within 5 s")


def test_probes_stay_out_of_the_access_log(
    client: TestClient, ops: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    ops.get("/livez")
    client.get("/api/solar")
    access = [r.getMessage() for r in caplog.records if r.name == "grelmicro.access"]
    assert access == ["GET /api/solar 200"]
