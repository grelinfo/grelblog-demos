"""Part 2: Two phones, one meal. Each test name ends with its rule id in SPEC.md."""

import importlib
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi.testclient import TestClient

from conftest import STEPS, Database

PASTA = {"recipe": "pasta"}
MEALS = "/plans/2026-W45/meals"


def test_setting_a_slot_twice_keeps_one_P2_R01(run, db: Database) -> None:
    app = run("4_add_meal")
    url = f"{app.url}/slots/2026-11-03/dinner"
    assert httpx.put(url, json={"recipe": "pasta"}).status_code == 200
    assert httpx.put(url, json={"recipe": "pasta"}).status_code == 200
    assert db.rows("SELECT recipe FROM slots") == [{"recipe": "pasta"}]


def test_plain_post_adds_twice_P2_R02(run, db: Database) -> None:
    app = run("4_add_meal")
    httpx.post(f"{app.url}{MEALS}", json=PASTA)
    httpx.post(f"{app.url}{MEALS}", json=PASTA)  # the same tap, sent again
    assert len(db.rows("SELECT * FROM meals")) == 2


def test_put_with_client_id_adds_once_P2_R03(run, db: Database) -> None:
    app = run("4_add_meal")
    url = f"{app.url}{MEALS}/{uuid.uuid4()}"
    first = httpx.put(url, json=PASTA)
    again = httpx.put(url, json=PASTA)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert len(db.rows("SELECT * FROM meals")) == 1


def test_same_id_at_once_adds_once_P2_R04(run, db: Database) -> None:
    app = run("4_add_meal")
    url = f"{app.url}{MEALS}/{uuid.uuid4()}"
    with ThreadPoolExecutor(max_workers=10) as pool:
        answers = list(pool.map(lambda _: httpx.put(url, json=PASTA), range(10)))
    assert {a.status_code for a in answers} == {200}
    assert len(db.rows("SELECT * FROM meals")) == 1


def test_same_id_other_meal_refused_P2_R05(run, db: Database) -> None:
    app = run("4_add_meal")
    url = f"{app.url}{MEALS}/{uuid.uuid4()}"
    assert httpx.put(url, json=PASTA).status_code == 200
    other = httpx.put(url, json={"recipe": "pizza"})
    assert other.status_code == 409
    assert db.rows("SELECT recipe FROM meals") == [{"recipe": "pasta"}]


def test_key_and_write_are_separate_P2_R07(postgres_url, db: Database, monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_URL", postgres_url)
    monkeypatch.syspath_prepend(str(STEPS))
    sys.modules.pop("4_add_meal", None)
    step = importlib.import_module("4_add_meal")
    with TestClient(step.app, raise_server_exceptions=False) as client:
        key = {"Idempotency-Key": "tap-1"}
        first = client.post(f"{MEALS}/keyed", json=PASTA, headers=key)
        again = client.post(f"{MEALS}/keyed", json=PASTA, headers=key)
        assert again.json() == first.json()  # without a crash, the key works
        assert len(db.rows("SELECT * FROM meals")) == 1

        added = step.Added

        def crash_before_store(**kwargs):  # the row is in, the answer is not stored
            monkeypatch.setattr(step, "Added", added)
            raise RuntimeError("the process dies here")

        monkeypatch.setattr(step, "Added", crash_before_store)
        key = {"Idempotency-Key": "tap-2"}
        assert client.post(f"{MEALS}/keyed", json=PASTA, headers=key).status_code == 500
        assert client.post(f"{MEALS}/keyed", json=PASTA, headers=key).status_code == 200
    assert len(db.rows("SELECT * FROM meals")) == 3  # tap-2 added the meal twice


def test_same_key_other_meal_refused_P2_R08(postgres_url, db: Database, monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_URL", postgres_url)
    monkeypatch.syspath_prepend(str(STEPS))
    sys.modules.pop("4_add_meal", None)
    step = importlib.import_module("4_add_meal")
    with TestClient(step.app) as client:
        key = {"Idempotency-Key": "tap-3"}
        assert client.post(f"{MEALS}/keyed", json=PASTA, headers=key).status_code == 200
        other = client.post(f"{MEALS}/keyed", json={"recipe": "pizza"}, headers=key)
        assert other.status_code == 409
    assert db.rows("SELECT recipe FROM meals") == [{"recipe": "pasta"}]
