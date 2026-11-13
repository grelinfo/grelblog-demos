import json
from datetime import date
from typing import Annotated, Literal
from uuid import UUID, uuid4

from db import (
    StoredMeal,
    family_emails,
    find_meal,
    insert_meal,
    mark_finished,
    set_slot,
)
from fastapi import FastAPI, Header, HTTPException
from grelmicro import Grelmicro
from grelmicro.idempotency import (
    Idempotency,
    IdempotencyConflictError,
)
from grelmicro.outbox import Message, Outbox
from grelmicro.providers.postgres import PostgresProvider
from mail import render, send_email
from pydantic import BaseModel

postgres = PostgresProvider()  # reads POSTGRES_URL
outbox = Outbox(postgres)  # a table in the same Postgres
micro = Grelmicro(uses=[postgres, outbox])
app = FastAPI()
micro.install(app)


# The intent, not the rendered email.
class PlanReady(BaseModel):
    to: str
    week: str


# Runs in the relay, retried with backoff on failure.
@outbox.handler(PlanReady)
async def send_plan_ready(message: Message[PlanReady]) -> None:
    assert message.data is not None
    mail = render("plan_ready", week=message.data.week)
    await send_email(message.data.to, mail)


class Plan(BaseModel):
    week: str


@app.post("/plans/{week}/finish")
async def finish_plan(week: str) -> Plan:
    async with (
        postgres.client.acquire() as conn,
        conn.transaction(),
    ):
        # A retry finds the plan finished: no new emails.
        if not await mark_finished(conn, week):
            return Plan(week=week)
        # One message per person, saved with the plan.
        for email in await family_emails(conn):
            await outbox.publish(
                conn, PlanReady(to=email, week=week)
            )
    return Plan(week=week)


class Slot(BaseModel):
    recipe: str


# One dinner per date: setting it twice keeps one row.
@app.put("/slots/{day}/{meal_type}")
async def put_slot(
    day: date, meal_type: Literal["lunch", "dinner"], slot: Slot
) -> Slot:
    async with postgres.client.acquire() as conn:
        await set_slot(conn, day, meal_type, slot.recipe)
    return slot


class Meal(BaseModel):
    recipe: str


class Added(BaseModel):
    id: UUID


# The first version: a new id, and a new row, on every call.
@app.post("/plans/{week}/meals")
async def add_meal(week: str, meal: Meal) -> Added:
    meal_id = uuid4()
    async with postgres.client.acquire() as conn:
        await insert_meal(conn, meal_id, week, meal.recipe)
    return Added(id=meal_id)


# Insert, or do nothing if the id is already there.
ADD_ONCE = """
INSERT INTO meals (id, week, recipe) VALUES ($1, $2, $3)
ON CONFLICT (id) DO NOTHING
"""


# The phone picks the id when the user taps.
@app.put("/plans/{week}/meals/{meal_id}")
async def put_meal(week: str, meal_id: UUID, meal: Meal) -> Meal:
    async with postgres.client.acquire() as conn:
        await conn.execute(ADD_ONCE, meal_id, week, meal.recipe)
        stored = await find_meal(conn, meal_id)
    if stored != StoredMeal(week=week, recipe=meal.recipe):
        # The same id for another meal: a bug in the app.
        raise HTTPException(409, "This meal id is already used")
    return meal


# Answers seen in the last hour, by key.
meal_keys = Idempotency[Added]("meal", ttl=3600)


# The same idea with a key next to the write.
@app.post("/plans/{week}/meals/keyed")
async def add_meal_keyed(
    week: str,
    meal: Meal,
    key: Annotated[str, Header(alias="Idempotency-Key")],
) -> Added:
    # The same key with another meal is refused, not replayed.
    fingerprint = json.dumps([week, meal.recipe])
    try:
        async with meal_keys(key, fingerprint=fingerprint) as op:
            if op.replayed:
                return op.result()
            meal_id = uuid4()
            async with postgres.client.acquire() as conn:
                await insert_meal(conn, meal_id, week, meal.recipe)
            # A failure here adds the meal again on retry.
            answer = Added(id=meal_id)
            op.store(answer)
            return answer
    except IdempotencyConflictError:
        raise HTTPException(409, "Key already used") from None
