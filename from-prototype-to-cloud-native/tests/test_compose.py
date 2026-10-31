"""compose.yaml on localhost:8080: what the posts claim about two replicas.

Needs Podman. Run with `uv run pytest -m compose`.
"""

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.compose

DEMO = Path(__file__).parent.parent
# Serve on localhost without TLS. The files shown in the posts stay as they are.
LOCAL = """\
services:
  caddy:
    ports: !override ["8080:80"]
    volumes: !override [./.Caddyfile.local:/etc/caddy/Caddyfile:ro]
"""


def compose(*args: str) -> str:
    return subprocess.run(
        ["podman", "compose", "-p", "demo-check"]
        + ["-f", "compose.yaml", "-f", ".compose.local.yaml", *args],
        cwd=DEMO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def write_local_caddyfile(switched: bool = False) -> None:
    caddyfile = (DEMO / "Caddyfile").read_text()
    caddyfile = caddyfile.replace("preview.myapp.example.com {", "http://preview.localhost {")
    caddyfile = caddyfile.replace("myapp.example.com {", "http://localhost {")
    if switched:
        caddyfile = caddyfile.replace("myapp-blue", "BLUE").replace("myapp-green", "myapp-blue")
        caddyfile = caddyfile.replace("BLUE", "myapp-green")
    (DEMO / ".Caddyfile.local").write_text(caddyfile)


@pytest.fixture(scope="module")
def web() -> Iterator[httpx.Client]:
    write_local_caddyfile()
    (DEMO / ".compose.local.yaml").write_text(LOCAL)
    subprocess.run(["podman", "build", "-q", "-t", "myapp", "."], cwd=DEMO, check=True)
    try:
        compose("up", "-d")
        time.sleep(35)  # room for 3 or 4 ticks of the 10 s task
        with httpx.Client(base_url="http://localhost:8080") as client:
            yield client
    finally:
        compose("down", "-v")
        (DEMO / ".Caddyfile.local").unlink()
        (DEMO / ".compose.local.yaml").unlink()


def test_the_app_answers_through_caddy(web: httpx.Client) -> None:
    assert web.get("/api/solar").status_code == 200


def test_metrics_stay_private(web: httpx.Client) -> None:
    assert web.get("/metrics").status_code == 404


def test_the_task_runs_once_per_tick(web: httpx.Client) -> None:
    logs = compose("logs", "myapp-blue", "myapp-green")
    # 35 s give 3 or 4 ticks. Without the claim, twice as many runs.
    assert 2 <= logs.count("water heater") <= 5


def test_the_preview_name_reaches_green_only(web: httpx.Client) -> None:
    preview = {"Host": "preview.localhost"}
    compose("stop", "myapp-blue")
    try:
        assert web.get("/api/solar", headers=preview).status_code == 200
        assert web.get("/api/solar").status_code == 502  # live is blue
    finally:
        compose("start", "myapp-blue")
        time.sleep(5)


def test_the_switch_moves_live_traffic_to_green(web: httpx.Client) -> None:
    write_local_caddyfile(switched=True)
    compose("exec", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile")
    compose("stop", "myapp-blue")
    assert {web.get("/api/solar").status_code for _ in range(10)} == {200}
