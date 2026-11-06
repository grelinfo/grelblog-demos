import logging
import re
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

DEMO = Path(__file__).parent.parent
HTML = {"accept": "text/html"}
STEPS = sorted((DEMO / "steps").glob("[0-9]_*.py"))


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


def test_probes_and_metrics_answer_on_the_ops_port(client: TestClient, ops: httpx.Client) -> None:
    assert ops.get("/livez").status_code == 200
    assert ops.get("/readyz").status_code == 200
    assert "# TYPE" in ops.get("/metrics").text
    assert client.get("/metrics", headers={"accept": "application/json"}).status_code == 404


def test_the_api_answers_behind_the_rate_limit_and_breaker(client: TestClient) -> None:
    assert client.get("/api/solar").json() == {"power_kw": 4.2}


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


def test_an_asleep_inverter_reads_zero_and_readiness_stays_up(
    day_four: TestClient, ops: httpx.Client
) -> None:
    main = sys.modules["main"]
    main.app.add_api_route("/test/isolate", main.breaker.isolate, methods=["POST"])
    day_four.post("/test/isolate")
    assert day_four.get("/api/solar").json() == {"power_kw": 0.0}
    assert ops.get("/readyz").status_code == 200
    assert 'grelmicro_circuit_breaker_name="inverter"' in ops.get("/metrics").text


def test_an_open_breaker_shows_in_healthz_and_leaves_readyz_alone(
    client: TestClient, ops: httpx.Client
) -> None:
    main = sys.modules["main"]
    main.app.add_api_route("/test/isolate", main.breaker.isolate, methods=["POST"])
    client.post("/test/isolate")
    healthz = ops.get("/healthz")
    assert healthz.status_code == 200
    check = healthz.json()["checks"]["circuitbreaker:inverter"]
    assert check["status"] == "error"
    assert check["critical"] is False
    assert ops.get("/readyz").status_code == 200


def test_breaker_calls_are_in_the_metrics(client: TestClient, ops: httpx.Client) -> None:
    client.get("/api/solar")
    metrics = ops.get("/metrics").text
    assert 'grelmicro_circuit_breaker_name="inverter"' in metrics


def test_an_open_breaker_falls_back_before_the_rate_limit(day_four: TestClient) -> None:
    main = sys.modules["main"]
    main.app.add_api_route("/test/isolate", main.breaker.isolate, methods=["POST"])
    day_four.post("/test/isolate")
    start = time.monotonic()
    answers = [day_four.get("/api/solar") for _ in range(10)]
    assert {a.status_code for a in answers} == {200}  # never 429: no token spent while open
    assert {a.json()["power_kw"] for a in answers} == {0.0}
    assert time.monotonic() - start < 1.0


def test_day_five_reads_the_inverter_once_for_many_calls(
    client: TestClient, ops: httpx.Client
) -> None:
    answers = {client.get("/api/solar").json()["power_kw"] for _ in range(20)}
    assert answers == {4.2}
    calls = re.search(
        r"grelmicro_circuit_breaker_calls_total\{[^}]*"
        r'grelmicro_circuit_breaker_name="inverter"[^}]*'
        r'grelmicro_outcome="success"[^}]*\} ([0-9.]+)',
        ops.get("/metrics").text,
    )
    assert calls is not None
    assert float(calls.group(1)) == 1  # the task and 20 requests share one cached read
