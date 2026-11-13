import asyncio
from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from db import (
    StoredMeal,
    family_emails,
    find_meal,
    mark_accepted,
    mark_bounced,
    mark_finished,
    save_sending,
    set_slot,
)
from fastapi import FastAPI, HTTPException
from grelmicro import Grelmicro
from grelmicro.http import OpsServer
from grelmicro.metrics import Metrics
from grelmicro.outbox import Message, Outbox
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.task import Tasks
from mail import delete_read, fetch_bounces, render, send_email
from pydantic import BaseModel

postgres = PostgresProvider()  # reads POSTGRES_URL
# Keep delivered rows a week, so keys still hold.
outbox = Outbox(postgres, keep_delivered=timedelta(days=7))
metrics = Metrics(exporter="prometheus")  # /metrics on :8080
tasks = Tasks()
micro = Grelmicro(
    uses=[postgres, outbox, metrics, OpsServer(), tasks]
)
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


# One email to the family for each new meal.
class MealAdded(BaseModel):
    meal_id: UUID
    week: str
    recipe: str
    to: list[str]


# Insert, or do nothing if the id is already there.
ADD_ONCE = """
INSERT INTO meals (id, week, recipe) VALUES ($1, $2, $3)
ON CONFLICT (id) DO NOTHING
"""


# The phone picks the id when the user taps.
@app.put("/plans/{week}/meals/{meal_id}")
async def put_meal(week: str, meal_id: UUID, meal: Meal) -> Meal:
    async with (
        postgres.client.acquire() as conn,
        conn.transaction(),
    ):
        await conn.execute(ADD_ONCE, meal_id, week, meal.recipe)
        stored = await find_meal(conn, meal_id)
        if stored != StoredMeal(week=week, recipe=meal.recipe):
            # The same id for another meal: a bug in the app.
            raise HTTPException(409, "This meal id is already used")
        added = MealAdded(
            meal_id=meal_id,
            week=week,
            recipe=meal.recipe,
            to=await family_emails(conn),
        )
        # A retry saves nothing: the key is taken.
        await outbox.publish(conn, added, dedup_key=str(meal_id))
    return meal


# Runs in the relay. A retry keeps the message id.
@outbox.handler(MealAdded)
async def send_meal_added(message: Message[MealAdded]) -> None:
    assert message.data is not None
    meal = message.data
    async with postgres.client.acquire() as conn:
        await save_sending(conn, message.id, meal.meal_id)
    mail = render("meal_added", week=meal.week, recipe=meal.recipe)
    await send_email(
        meal.to,
        mail,
        # Bounces come back to this address.
        sender=f"bounces+{message.id}@planner.example.com",
        message_id=f"<{message.id}@planner.example.com>",
    )
    async with postgres.client.acquire() as conn:
        await mark_accepted(conn, message.id)


# One replica reads the mailbox each minute.
@tasks.every(interval=60, gate="claim")
async def read_bounces() -> None:
    bounces = await asyncio.to_thread(fetch_bounces)
    changed = 0
    async with postgres.client.acquire() as conn:
        for bounce in bounces:
            # Out-of-office replies land here too.
            if bounce.failed and bounce.message_id:
                changed += await mark_bounced(
                    conn, bounce.message_id
                )
    # Delete only once the database has them.
    await asyncio.to_thread(delete_read, bounces)
    metrics.counter("emails.bounced").add(changed)
